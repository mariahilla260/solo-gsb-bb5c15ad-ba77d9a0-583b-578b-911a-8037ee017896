"""SQLite 存储层。

四张表：

* ``messages``：一封邮件一行，保存指纹、原始字节哈希、规范化 JSON、
  根节点信息与时间戳；
* ``nodes``：MIME 树节点，按 :attr:`MIMENode.path` 物化路径存储，
  可直接按 ``message_id + path`` 查询任意节点；
* ``attachments``：内容寻址附件库——所有非文本叶子的解码后字节按
  SHA-256 去重，一份内容只存一行；
* ``attachment_refs``：附件出现位置（哪封邮件的哪个节点、解码后的
  文件名与 disposition），同一内容可被多处引用。

写入在 *单个事务* 中完成：解析失败时根本不会进入此模块；写入中途
出错会整体回滚，数据库里不会留下半成品。重复归档同一指纹的邮件
不会新增任何行（包括附件实体与引用）。

新表全部用 ``CREATE TABLE IF NOT EXISTS`` 建立，启动时对已有库文件
幂等升级；升级前归档的邮件没有留存附件字节，不会回溯补录。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Optional

from .mimeparser import MIMENode

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT NOT NULL UNIQUE,
    raw_sha256      TEXT NOT NULL,
    canonical_json  TEXT NOT NULL,
    content_type    TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL
                        REFERENCES messages(id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    depth           INTEGER NOT NULL,
    position        INTEGER NOT NULL,
    content_type    TEXT NOT NULL,
    kind            TEXT NOT NULL,
    cte             TEXT NOT NULL,
    disposition     TEXT,
    headers_json    TEXT NOT NULL,
    children_json   TEXT NOT NULL,
    text_content    TEXT,
    blob_sha256     TEXT,
    blob_length     INTEGER,
    UNIQUE(message_id, path)
);

CREATE INDEX IF NOT EXISTS idx_nodes_message ON nodes(message_id);
CREATE INDEX IF NOT EXISTS idx_messages_fingerprint ON messages(fingerprint);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);

CREATE TABLE IF NOT EXISTS attachments (
    sha256          TEXT PRIMARY KEY,
    size_bytes      INTEGER NOT NULL,
    media_type      TEXT NOT NULL,
    content         BLOB NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachment_refs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    attachment_sha256   TEXT NOT NULL
                            REFERENCES attachments(sha256) ON DELETE CASCADE,
    message_id          INTEGER NOT NULL
                            REFERENCES messages(id) ON DELETE CASCADE,
    node_path           TEXT NOT NULL,
    filename            TEXT,
    disposition         TEXT,
    media_type          TEXT NOT NULL,
    UNIQUE(attachment_sha256, message_id, node_path)
);

CREATE INDEX IF NOT EXISTS idx_attachment_refs_attachment
    ON attachment_refs(attachment_sha256);
CREATE INDEX IF NOT EXISTS idx_attachment_refs_message
    ON attachment_refs(message_id);
CREATE INDEX IF NOT EXISTS idx_attachment_refs_filename
    ON attachment_refs(filename);
"""


def connect(db_path: str) -> sqlite3.Connection:
    # check_same_thread=False：同步依赖在工作线程建连、async 端点在事件
    # 循环线程使用；每个请求独占一个连接、不会并发，故安全。
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    """启动时调用：建库建表（幂等）。"""

    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        # WAL 让读写并发更顺滑（同一卷不支持时 SQLite 会自动回退）。
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def save_message(
    conn: sqlite3.Connection,
    *,
    fingerprint: str,
    raw_hash: str,
    canonical: bytes,
    size: int,
    root: MIMENode,
) -> tuple[int, bool]:
    """原子保存邮件与整棵 MIME 树。

    相同指纹的邮件重复提交时幂等：直接返回已有 id，不重复写入。
    返回 ``(message_id, created)``。两个相同指纹并发提交时，靠
    ``messages.fingerprint`` 的 UNIQUE 约束兜底，后到者转为幂等返回。
    """

    try:
        return _insert_message(
            conn,
            fingerprint=fingerprint,
            raw_hash=raw_hash,
            canonical=canonical,
            size=size,
            root=root,
        )
    except sqlite3.IntegrityError:
        # 并发竞态：另一个请求已插入相同指纹。回滚本事务后读回。
        conn.rollback()
        row = conn.execute(
            "SELECT id FROM messages WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if row is None:
            raise
        return int(row["id"]), False


def _insert_message(
    conn: sqlite3.Connection,
    *,
    fingerprint: str,
    raw_hash: str,
    canonical: bytes,
    size: int,
    root: MIMENode,
) -> tuple[int, bool]:
    with transaction(conn):
        existing = conn.execute(
            "SELECT id FROM messages WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if existing is not None:
            return int(existing["id"]), False

        cur = conn.execute(
            """
            INSERT INTO messages
                (fingerprint, raw_sha256, canonical_json, content_type,
                 size_bytes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                raw_hash,
                canonical.decode("utf-8"),
                root.media_type,
                size,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        message_id = int(cur.lastrowid)
        for node, depth, position in _walk(root):
            _insert_node(conn, message_id, node, depth, position)
        return message_id, True


def _walk(root: MIMENode) -> Iterator[tuple[MIMENode, int, int]]:
    stack: list[tuple[MIMENode, int, int]] = [(root, 0, 0)]
    while stack:
        node, depth, position = stack.pop()
        yield node, depth, position
        # 逆序压栈以保持 1..n 的访问顺序
        for i in range(len(node.children) - 1, -1, -1):
            stack.append((node.children[i], depth + 1, i + 1))


def _insert_node(
    conn: sqlite3.Connection,
    message_id: int,
    node: MIMENode,
    depth: int,
    position: int,
) -> None:
    # 头值在节点构建时已完成 RFC 2047 解码并校验成功，直接存储；
    # 头名保留原始大小写。
    headers = [
        [h.raw_name.decode("ascii", "replace"), h.decoded]
        for h in node.headers
    ]
    children = [c.path for c in node.children]

    blob_hash = None
    blob_len = None
    if not node.is_leaf:
        kind = "multipart" if node.type == "multipart" else "message"
    elif node.is_text:
        kind = "text"
    else:
        kind = "blob"
        blob = node.blob or b""
        blob_hash = hashlib.sha256(blob).hexdigest()
        blob_len = len(blob)

    conn.execute(
        """
        INSERT INTO nodes
            (message_id, path, depth, position, content_type, kind, cte,
             disposition, headers_json, children_json, text_content,
             blob_sha256, blob_length)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            node.path,
            depth,
            position,
            node.media_type,
            kind,
            node.cte,
            node.disposition,
            json.dumps(headers, ensure_ascii=False),
            json.dumps(children),
            node.text,
            blob_hash,
            blob_len,
        ),
    )

    if kind == "blob":
        assert blob_hash is not None
        _insert_attachment(conn, message_id, node, node.blob or b"", blob_hash)


def _insert_attachment(
    conn: sqlite3.Connection,
    message_id: int,
    node: MIMENode,
    blob: bytes,
    blob_hash: str,
) -> None:
    """把非文本叶子的解码字节写入内容寻址附件库，并登记本次出现。

    实体按 SHA-256 去重（``INSERT OR IGNORE``，已存在则保留首次写入的
    行）；引用按 ``(sha256, message_id, node_path)`` 唯一，重复归档同一
    邮件在指纹层面就已短路，不会走到这里。
    """

    conn.execute(
        """
        INSERT OR IGNORE INTO attachments
            (sha256, size_bytes, media_type, content, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            blob_hash,
            len(blob),
            node.media_type,
            blob,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    # 解码文件名：优先 Content-Disposition 的 filename，回退
    # Content-Type 的 name；两者在解析阶段已完成 RFC 2231 解码。
    filename = node.disposition_params.get("filename") or (
        node.content_type_params.get("name")
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO attachment_refs
            (attachment_sha256, message_id, node_path, filename,
             disposition, media_type)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            blob_hash,
            message_id,
            node.path,
            filename,
            node.disposition,
            node.media_type,
        ),
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def get_message(conn: sqlite3.Connection, message_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE id = ?", (message_id,)
    ).fetchone()


def get_message_by_fingerprint(
    conn: sqlite3.Connection, fingerprint: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()


def list_messages(
    conn: sqlite3.Connection, *, limit: int, offset: int
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT id, fingerprint, raw_sha256, content_type, size_bytes,
                   created_at
            FROM messages ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    )


def count_messages(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"])


def get_node(
    conn: sqlite3.Connection, message_id: int, path: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM nodes WHERE message_id = ? AND path = ?",
        (message_id, path),
    ).fetchone()


def list_nodes(conn: sqlite3.Connection, message_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM nodes WHERE message_id = ? ORDER BY id",
            (message_id,),
        ).fetchall()
    )


def list_thread_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """全部归档邮件的会话构建输入：id、归档时间、根节点头字段。

    每封邮件恰好一行（解析器保证每封邮件存在 path='' 的根节点），
    按归档 id 升序返回，供会话关联查询在全量归档上构建回复图。
    """

    return list(
        conn.execute(
            """
            SELECT m.id AS id, m.created_at AS created_at,
                   n.headers_json AS headers_json
            FROM messages m
            JOIN nodes n ON n.message_id = m.id AND n.path = ''
            ORDER BY m.id
            """
        ).fetchall()
    )


def list_calendar_nodes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """全部归档邮件中的 ``text/calendar`` 文本叶子（含封装报文内部）。

    供 ``GET /calendar/events`` 做全库只读汇总。按归档 id 与节点
    id（即邮件内 MIME 文档序）升序返回；调用方还需按
    ``(message_id, path)`` 聚合后自行按物化路径排文档序。
    """

    return list(
        conn.execute(
            """
            SELECT n.message_id AS message_id, n.path AS path,
                   n.content_type AS content_type, n.kind AS kind,
                   n.text_content AS text_content
            FROM nodes n
            WHERE n.kind = 'text'
              AND lower(n.content_type) = 'text/calendar'
            ORDER BY n.message_id, n.id
            """
        ).fetchall()
    )


# ---------------------------------------------------------------------------
# 附件库查询
# ---------------------------------------------------------------------------


def get_attachment(conn: sqlite3.Connection, sha256: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT sha256, size_bytes, media_type, content, created_at
        FROM attachments WHERE sha256 = ?
        """,
        (sha256,),
    ).fetchone()


def list_message_attachment_refs(
    conn: sqlite3.Connection, message_id: int
) -> list[sqlite3.Row]:
    """某封邮件的全部附件出现位置，按节点 id（即 MIME 文档序）。

    每个 blob 叶子一行（同哈希多次出现不合并），供整邮件 ZIP 打包使用。
    """

    return list(
        conn.execute(
            """
            SELECT attachment_sha256, node_path, filename, disposition,
                   media_type
            FROM attachment_refs
            WHERE message_id = ?
            ORDER BY (SELECT id FROM nodes n
                     WHERE n.message_id = attachment_refs.message_id
                       AND n.path = attachment_refs.node_path)
            """,
            (message_id,),
        ).fetchall()
    )


def list_attachment_refs(
    conn: sqlite3.Connection, sha256: str
) -> list[sqlite3.Row]:
    """某附件的全部出现位置，按归档写入顺序（即邮件内文档序）。"""

    return list(
        conn.execute(
            """
            SELECT message_id, node_path, filename, disposition, media_type
            FROM attachment_refs
            WHERE attachment_sha256 = ? ORDER BY id
            """,
            (sha256,),
        ).fetchall()
    )


def _like_escape(value: str) -> str:
    """转义 LIKE 模式中的特殊字符（配合 ``ESCAPE '\\'``）。"""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _attachment_where(
    sha256: Optional[str],
    filename: Optional[str],
    media_type: Optional[str],
) -> tuple[str, list[str]]:
    """组装附件列表的 WHERE 子句与参数（仅内部常量拼接，用户输入全走占位符）。"""

    clauses: list[str] = []
    params: list[str] = []
    if sha256:
        # 前缀匹配：传完整 64 位哈希即精确查找
        clauses.append("a.sha256 LIKE ? ESCAPE '\\'")
        params.append(_like_escape(sha256.lower()) + "%")
    if filename:
        # 任一出现位置的文件名包含该子串即命中
        clauses.append(
            "EXISTS (SELECT 1 FROM attachment_refs r"
            " WHERE r.attachment_sha256 = a.sha256"
            " AND r.filename LIKE ? ESCAPE '\\')"
        )
        params.append(f"%{_like_escape(filename)}%")
    if media_type:
        # 任一出现位置以该媒体类型归档即命中
        clauses.append(
            "EXISTS (SELECT 1 FROM attachment_refs r"
            " WHERE r.attachment_sha256 = a.sha256"
            " AND r.media_type = ?)"
        )
        params.append(media_type.lower())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def list_attachments(
    conn: sqlite3.Connection,
    *,
    limit: int,
    offset: int,
    sha256: Optional[str] = None,
    filename: Optional[str] = None,
    media_type: Optional[str] = None,
) -> list[sqlite3.Row]:
    where, params = _attachment_where(sha256, filename, media_type)
    sql = (
        "SELECT a.sha256, a.size_bytes, a.media_type, a.created_at,"
        "       (SELECT COUNT(*) FROM attachment_refs r"
        "        WHERE r.attachment_sha256 = a.sha256) AS reference_count"
        " FROM attachments a"
        + where
        + " ORDER BY a.created_at DESC, a.sha256 LIMIT ? OFFSET ?"
    )
    return list(conn.execute(sql, (*params, limit, offset)).fetchall())


def count_attachments(
    conn: sqlite3.Connection,
    *,
    sha256: Optional[str] = None,
    filename: Optional[str] = None,
    media_type: Optional[str] = None,
) -> int:
    where, params = _attachment_where(sha256, filename, media_type)
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM attachments a" + where, params
    ).fetchone()
    return int(row["c"])


# ---------------------------------------------------------------------------
# 批量撤销归档（预览影响计算 + 单事务删除）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnarchiveTargetInfo:
    """撤销影响中的一封目标邮件。"""

    id: int
    fingerprint: str
    content_type: str
    size_bytes: int
    node_count: int
    attachment_refs_removed: int


@dataclass(frozen=True)
class UnarchiveAttachmentInfo:
    """撤销后无人引用、可被清理的附件实体。"""

    sha256: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True)
class UnarchivePlan:
    """预览与执行共用的影响计算结果。

    :param targets: 目标邮件，按 id 升序
    :param orphans: 本批删除后无任何剩余引用的附件，按 sha256 升序
    """

    targets: list[UnarchiveTargetInfo]
    orphans: list[UnarchiveAttachmentInfo]

    @property
    def total_refs_removed(self) -> int:
        return sum(t.attachment_refs_removed for t in self.targets)

    @property
    def reclaimed_bytes(self) -> int:
        return sum(a.size_bytes for a in self.orphans)


class UnarchiveConflict(Exception):
    """事务内复核发现目标行已被并发改写/删除（整批回滚，未改动数据）。"""


def _fetch_messages_for_unarchive(
    conn: sqlite3.Connection, message_ids: list[int]
) -> dict[int, sqlite3.Row]:
    """按 id 批量读取邮件行；调用方须保证 message_ids 非空、无重复。"""

    placeholders = ",".join("?" for _ in message_ids)
    rows = conn.execute(
        f"SELECT * FROM messages WHERE id IN ({placeholders})",
        message_ids,
    ).fetchall()
    return {int(r["id"]): r for r in rows}


def build_unarchive_plan(
    conn: sqlite3.Connection, message_ids: list[int]
) -> UnarchivePlan:
    """只读计算撤销一批邮件的影响（预览与执行共用同一口径）。

    调用方负责事先校验 id 存在且指纹匹配。结果：

    * ``targets``：目标邮件按 **id 升序**，含将移除的节点数
      （``node_count``）与该邮件的附件引用数（blob 叶子数）；
    * ``orphans``：仅当**本批全部目标删除后**，在
      ``attachment_refs`` 中不再有任何引用行的附件，按 sha256 升序；
      仍被非目标邮件引用的附件不出现，其字节保持可查可下载。
    """

    msg_rows = _fetch_messages_for_unarchive(conn, message_ids)
    targets: list[UnarchiveTargetInfo] = []
    for mid in sorted(message_ids):
        row = msg_rows[mid]
        node_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM nodes WHERE message_id = ?",
                (mid,),
            ).fetchone()["c"]
        )
        ref_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM attachment_refs WHERE message_id = ?",
                (mid,),
            ).fetchone()["c"]
        )
        targets.append(
            UnarchiveTargetInfo(
                id=mid,
                fingerprint=row["fingerprint"],
                content_type=row["content_type"],
                size_bytes=int(row["size_bytes"]),
                node_count=node_count,
                attachment_refs_removed=ref_count,
            )
        )

    placeholders = ",".join("?" for _ in message_ids)
    orphan_rows = conn.execute(
        f"""
        SELECT a.sha256 AS sha256, a.size_bytes AS size_bytes,
               a.media_type AS media_type
        FROM attachments a
        WHERE EXISTS (
                  SELECT 1 FROM attachment_refs r
                  WHERE r.attachment_sha256 = a.sha256
                    AND r.message_id IN ({placeholders})
              )
          AND NOT EXISTS (
                  SELECT 1 FROM attachment_refs r
                  WHERE r.attachment_sha256 = a.sha256
                    AND r.message_id NOT IN ({placeholders})
              )
        ORDER BY a.sha256
        """,
        (*message_ids, *message_ids),
    ).fetchall()
    orphans = [
        UnarchiveAttachmentInfo(
            sha256=r["sha256"],
            size_bytes=int(r["size_bytes"]),
            media_type=r["media_type"],
        )
        for r in orphan_rows
    ]
    return UnarchivePlan(targets=targets, orphans=orphans)


def delete_messages(conn: sqlite3.Connection, message_ids: list[int]) -> UnarchivePlan:
    """在**单个事务**内撤销一批邮件，返回事务内确认的实际影响计划。

    删除顺序：先 ``attachment_refs``（本批目标的引用行）→ 再 ``nodes``
    → 再 ``messages``（外键 ``ON DELETE CASCADE`` 仅作兜底）→ 最后只删除
    引用计数归零的 ``attachments`` 实体。仍被其他邮件引用的附件字节不动。

    目标计数（节点数/引用数）与将清理的附件集在**同一事务、同一保留锁**
    内重新计算并校验，因此返回的 :class:`UnarchivePlan` 与真实落库结果
    严格一致（也与执行前的预览同口径，二者不一致即抛
    :class:`UnarchiveConflict` 并整体回滚）。

    显式 ``BEGIN IMMEDIATE`` 在事务开始即取得保留锁，使“复核 → 删除 →
    清理”全程与其他写请求串行化；为避免 Python sqlite3 旧版隔离模式在
    显式事务前自动注入隐式 ``BEGIN``，这里临时切换到
    ``isolation_level=None`` 自行管理事务。
    """

    ordered_ids = sorted(set(message_ids))
    placeholders = ",".join("?" for _ in ordered_ids)

    prev_isolation = conn.isolation_level
    conn.isolation_level = None  # autocommit：模块不再自动注入 BEGIN/COMMIT
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            plan = _delete_messages_locked(conn, ordered_ids, placeholders)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.isolation_level = prev_isolation
    return plan


def _delete_messages_locked(
    conn: sqlite3.Connection, ordered_ids: list[int], placeholders: str
) -> UnarchivePlan:
    """``BEGIN IMMEDIATE`` 之后的实际删除步骤（调用方负责提交/回滚）。"""

    # 1) 事务内复核：目标仍全部存在（缺失说明与并发请求竞争）。
    found = {
        int(r["id"])
        for r in conn.execute(
            f"SELECT id FROM messages WHERE id IN ({placeholders})",
            ordered_ids,
        ).fetchall()
    }
    if len(found) != len(ordered_ids):
        missing = [i for i in ordered_ids if i not in found]
        raise UnarchiveConflict(
            f"事务内复核失败：邮件 {missing} 已不存在（可能被并发撤销）"
        )

    # 2) 锁内重新计算影响（与预览同口径），作为最终响应的权威数据。
    plan = build_unarchive_plan(conn, ordered_ids)
    orphan_digests = [a.sha256 for a in plan.orphans]

    # 3) 删除本批目标的附件引用行、MIME 节点行与邮件行。
    conn.execute(
        f"DELETE FROM attachment_refs WHERE message_id IN ({placeholders})",
        ordered_ids,
    )
    conn.execute(
        f"DELETE FROM nodes WHERE message_id IN ({placeholders})",
        ordered_ids,
    )
    cur = conn.execute(
        f"DELETE FROM messages WHERE id IN ({placeholders})",
        ordered_ids,
    )
    if cur.rowcount != len(ordered_ids):
        # 已持保留锁，复核与删除之间不可能插入并发写；命中说明不变量
        # 被破坏，整体回滚。
        raise UnarchiveConflict(
            f"预期删除 {len(ordered_ids)} 封邮件，实际 {cur.rowcount} 封"
        )

    # 4) 仅清理引用已归零的附件实体；存在剩余引用者绝不动。
    if orphan_digests:
        oph = ",".join("?" for _ in orphan_digests)
        deleted = conn.execute(
            f"""
            DELETE FROM attachments
            WHERE sha256 IN ({oph})
              AND NOT EXISTS (
                      SELECT 1 FROM attachment_refs r
                      WHERE r.attachment_sha256 = attachments.sha256
                  )
            """,
            orphan_digests,
        ).rowcount
        if deleted != len(orphan_digests):
            raise UnarchiveConflict(
                f"预期清理 {len(orphan_digests)} 个无引用附件，"
                f"实际 {deleted} 个"
            )
    return plan
