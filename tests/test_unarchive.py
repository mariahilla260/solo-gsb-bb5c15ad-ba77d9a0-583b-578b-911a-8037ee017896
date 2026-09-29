"""批量撤销归档（POST /messages/unarchive）测试。

覆盖：预览不变更、执行删除、共享附件保留、影响计算口径（id/sha256 升序、
引用数、可回收字节）、整批拒绝（空列表/重复 id/缺失/指纹不符）、
失败回滚与撤销后既有查询/下载行为。
"""

import base64
import hashlib

from conftest import make_multipart, text_part


def bin_part(
    data: bytes,
    *,
    ctype: str = "application/octet-stream",
    filename: str | None = None,
    disposition: str | None = "attachment",
    cte: str | None = "base64",
) -> bytes:
    ct = f"Content-Type: {ctype}"
    lines = [ct]
    if disposition is not None:
        disp = f"Content-Disposition: {disposition}"
        if filename is not None:
            disp += f'; filename="{filename}"'
        lines.append(disp)
    if cte is not None:
        lines.append(f"Content-Transfer-Encoding: {cte}")
    head = "\r\n".join(lines).encode() + b"\r\n\r\n"
    body = base64.b64encode(data) if cte == "base64" else data
    return head + body


def message_with_parts(*parts: bytes, subject: str = "attach test") -> bytes:
    return (
        f"Subject: {subject}\r\n".encode()
        + b"Content-Type: multipart/mixed; boundary=MIX\r\n\r\n"
        + make_multipart(list(parts), "MIX")
    )


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(32))
PDF = b"%PDF-1.4 fake" * 3


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def post(client, data: bytes) -> dict:
    r = client.post("/messages", content=data)
    assert r.status_code == 201, r.text
    return r.json()


def unarchive(client, items, *, preview=None):
    body = {"items": items}
    if preview is not None:
        body["preview"] = preview
    return client.post("/messages/unarchive", json=body)


def refs_of(client, digest: str) -> list[dict]:
    listing = client.get(f"/attachments?sha256={digest}").json()
    assert listing["total"] <= 1
    return listing["items"][0]["occurrences"] if listing["items"] else []


# ---------------------------------------------------------------------------
# 预览：不改动任何数据，与执行同口径
# ---------------------------------------------------------------------------


def test_preview_reports_impact_without_changes(client):
    m = post(client, message_with_parts(bin_part(PNG, filename="a.png")))
    before_total = client.get("/messages").json()["total"]
    before_atts = client.get("/attachments").json()["total"]

    r = unarchive(client, [{"id": m["id"], "fingerprint": m["fingerprint"]}],
                  preview=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["preview"] is True
    assert body["executed"] is False
    assert [t["id"] for t in body["targets"]] == [m["id"]]
    target = body["targets"][0]
    assert target["fingerprint"] == m["fingerprint"]
    assert target["content_type"] == "multipart/mixed"
    assert target["node_count"] == 2  # 根 + blob 叶子
    assert target["attachment_refs_removed"] == 1
    assert [a["sha256"] for a in body["attachments_removed"]] == [sha(PNG)]
    assert body["reclaimed_bytes"] == len(PNG)
    assert body["attachment_refs_removed"] == 1

    # 预览后数据原样：邮件、节点、附件实体与引用都还在
    assert client.get("/messages").json()["total"] == before_total
    assert client.get("/attachments").json()["total"] == before_atts
    assert client.get(f"/messages/{m['id']}").status_code == 200
    assert len(refs_of(client, sha(PNG))) == 1


def test_preview_works_for_text_only_message(client):
    m = post(client, b"Subject: t\r\n\r\nbody\r\n")
    r = unarchive(client, [{"id": m["id"], "fingerprint": m["fingerprint"]}],
                  preview=True)
    assert r.status_code == 200
    body = r.json()
    assert body["targets"][0]["node_count"] == 1
    assert body["targets"][0]["attachment_refs_removed"] == 0
    assert body["attachments_removed"] == []
    assert body["reclaimed_bytes"] == 0


# ---------------------------------------------------------------------------
# 执行：单事务删除目标邮件/节点/引用
# ---------------------------------------------------------------------------


def test_execute_deletes_message_nodes_refs_and_orphan_blob(client):
    m = post(client, message_with_parts(bin_part(PNG, filename="a.png")))
    r = unarchive(client, [{"id": m["id"], "fingerprint": m["fingerprint"]}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["preview"] is False
    assert body["executed"] is True
    assert body["reclaimed_bytes"] == len(PNG)

    # 邮件与节点消失
    assert client.get(f"/messages/{m['id']}").status_code == 404
    assert client.get(f"/messages/{m['id']}/nodes").status_code == 404
    assert client.get("/messages").json()["total"] == 0

    # 无引用的附件实体被清理，下载 404
    assert client.get("/attachments").json()["total"] == 0
    assert client.get(f"/attachments/{sha(PNG)}/content").status_code == 404


def test_execute_defaults_to_non_preview(client):
    m = post(client, b"Subject: t\r\n\r\nx\r\n")
    # 不传 preview
    r = client.post(
        "/messages/unarchive",
        json={"items": [{"id": m["id"], "fingerprint": m["fingerprint"]}]},
    )
    assert r.status_code == 200
    assert r.json()["executed"] is True
    assert client.get(f"/messages/{m['id']}").status_code == 404


def test_same_blob_twice_in_one_message_reclaimed_once(client):
    m = post(
        client,
        message_with_parts(
            bin_part(PNG, filename="x.png"),
            bin_part(PNG, filename="y.png"),
        ),
    )
    r = unarchive(client, [{"id": m["id"], "fingerprint": m["fingerprint"]}],
                  preview=True)
    body = r.json()
    assert body["targets"][0]["attachment_refs_removed"] == 2
    # 一个实体只回收一次字节
    assert len(body["attachments_removed"]) == 1
    assert body["reclaimed_bytes"] == len(PNG)

    assert unarchive(
        client, [{"id": m["id"], "fingerprint": m["fingerprint"]}]
    ).status_code == 200
    assert client.get("/attachments").json()["total"] == 0


# ---------------------------------------------------------------------------
# 共享附件：仅删除本批后无人引用的附件，仍被引用者保留可下载
# ---------------------------------------------------------------------------


def test_shared_blob_kept_when_other_message_still_references_it(client):
    m1 = post(
        client,
        message_with_parts(
            bin_part(PNG, filename="one.png"), bin_part(PDF, filename="p.pdf"),
            subject="m1",
        ),
    )
    m2 = post(
        client,
        message_with_parts(
            text_part("hi"), bin_part(PNG, filename="two.png"), subject="m2"
        ),
    )

    # 只撤销 m1：PDF 独占→清理；PNG 仍被 m2 引用→保留
    r = unarchive(client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}],
                  preview=True)
    body = r.json()
    assert [a["sha256"] for a in body["attachments_removed"]] == [sha(PDF)]
    assert body["reclaimed_bytes"] == len(PDF)
    t = body["targets"][0]
    assert t["attachment_refs_removed"] == 2
    assert body["attachment_refs_removed"] == 2

    assert unarchive(
        client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}]
    ).status_code == 200

    # m1 消失；PNG 实体保留，引用只剩 m2，字节可下载
    assert client.get(f"/messages/{m1['id']}").status_code == 404
    assert client.get(f"/attachments/{sha(PNG)}/content").status_code == 200
    content = client.get(f"/attachments/{sha(PNG)}/content").content
    assert content == PNG
    occ = refs_of(client, sha(PNG))
    assert [o["message_id"] for o in occ] == [m2["id"]]
    assert client.get(f"/attachments/{sha(PDF)}/content").status_code == 404

    # m2 的既有查询全部可用（归档、查询不回归）
    nodes = client.get(f"/messages/{m2['id']}/nodes").json()["nodes"]
    assert [n["path"] for n in nodes] == ["", "1", "2"]

    # 再撤销 m2：PNG 变为无人引用，被清理
    r2 = unarchive(client, [{"id": m2["id"], "fingerprint": m2["fingerprint"]}])
    assert r2.status_code == 200
    assert [a["sha256"] for a in r2.json()["attachments_removed"]] == [sha(PNG)]
    assert client.get("/attachments").json()["total"] == 0


def test_blob_shared_only_within_batch_is_reclaimed(client):
    m1 = post(client, message_with_parts(bin_part(PNG), subject="m1"))
    m2 = post(client, message_with_parts(bin_part(PNG), subject="m2"))
    r = unarchive(
        client,
        [
            {"id": m2["id"], "fingerprint": m2["fingerprint"]},
            {"id": m1["id"], "fingerprint": m1["fingerprint"]},
        ],
    )
    assert r.status_code == 200
    body = r.json()
    # 仅在“本批全部删除后”无人引用 → PNG 应回收一次
    assert [a["sha256"] for a in body["attachments_removed"]] == [sha(PNG)]
    assert body["reclaimed_bytes"] == len(PNG)
    assert client.get("/attachments").json()["total"] == 0


# ---------------------------------------------------------------------------
# 批量、排序与幂等语义
# ---------------------------------------------------------------------------


def test_batch_targets_sorted_by_id_and_attachments_by_sha(client):
    m1 = post(client, message_with_parts(bin_part(PDF), subject="m1"))
    m2 = post(client, message_with_parts(bin_part(PNG), subject="m2"))
    m3 = post(client, b"Subject: t\r\n\r\nz\r\n")

    r = unarchive(
        client,
        [
            {"id": m3["id"], "fingerprint": m3["fingerprint"]},
            {"id": m1["id"], "fingerprint": m1["fingerprint"]},
            {"id": m2["id"], "fingerprint": m2["fingerprint"]},
        ],
        preview=True,
    )
    body = r.json()
    # 无论请求顺序，目标按 id 升序
    assert [t["id"] for t in body["targets"]] == [m1["id"], m2["id"], m3["id"]]
    # 附件按 sha256 升序
    digests = [a["sha256"] for a in body["attachments_removed"]]
    assert digests == sorted(digests) == [sha(PDF), sha(PNG)]
    assert body["reclaimed_bytes"] == len(PDF) + len(PNG)
    assert body["attachment_refs_removed"] == 2


def test_repeated_execute_second_call_is_404_and_data_unchanged(client):
    m1 = post(client, message_with_parts(bin_part(PNG), subject="m1"))
    m2 = post(client, message_with_parts(bin_part(PNG), subject="m2"))
    assert unarchive(
        client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}]
    ).status_code == 200
    # 再次撤销已删除的 m1 → 404 整批拒绝，m2 与 PNG 不受影响
    r = unarchive(
        client,
        [
            {"id": m1["id"], "fingerprint": m1["fingerprint"]},
            {"id": m2["id"], "fingerprint": m2["fingerprint"]},
        ],
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "UNARCHIVE_MESSAGE_NOT_FOUND"
    assert r.json()["error"]["detail"]["ids"] == [m1["id"]]
    assert client.get(f"/messages/{m2['id']}").status_code == 200
    assert client.get(f"/attachments/{sha(PNG)}/content").status_code == 200


# ---------------------------------------------------------------------------
# 整批拒绝：空列表 / 重复 id / 缺失 / 指纹不符
# ---------------------------------------------------------------------------


def test_empty_batch_rejected_422(client):
    r = client.post("/messages/unarchive", json={"items": []})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNARCHIVE_EMPTY_BATCH"


def test_missing_items_field_rejected_422(client):
    r = client.post("/messages/unarchive", json={})
    assert r.status_code == 422


def test_duplicate_ids_rejected_422(client):
    m = post(client, b"Subject: t\r\n\r\nx\r\n")
    r = unarchive(
        client,
        [
            {"id": m["id"], "fingerprint": m["fingerprint"]},
            {"id": m["id"], "fingerprint": m["fingerprint"]},
        ],
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "UNARCHIVE_DUPLICATE_IDS"
    assert err["detail"]["ids"] == [m["id"]]
    # 数据不变
    assert client.get(f"/messages/{m['id']}").status_code == 200


def test_missing_id_rejected_404(client):
    m = post(client, b"Subject: t\r\n\r\nx\r\n")
    r = unarchive(
        client,
        [
            {"id": 999, "fingerprint": "a" * 64},
            {"id": m["id"], "fingerprint": m["fingerprint"]},
        ],
    )
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "UNARCHIVE_MESSAGE_NOT_FOUND"
    assert err["detail"]["ids"] == [999]
    assert client.get(f"/messages/{m['id']}").status_code == 200


def test_fingerprint_mismatch_rejected_409(client):
    m1 = post(client, message_with_parts(bin_part(PNG), subject="m1"))
    m2 = post(client, message_with_parts(bin_part(PNG), subject="m2"))
    wrong = "0" * 64
    r = unarchive(
        client,
        [
            {"id": m1["id"], "fingerprint": wrong},
            {"id": m2["id"], "fingerprint": m2["fingerprint"]},
        ],
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "UNARCHIVE_FINGERPRINT_MISMATCH"
    assert err["detail"]["items"] == [
        {
            "id": m1["id"],
            "fingerprint": wrong,
            "current_fingerprint": m1["fingerprint"],
        }
    ]
    # 整批不变：两封邮件都在，PNG 仍有两个引用
    assert client.get(f"/messages/{m1['id']}").status_code == 200
    assert client.get(f"/messages/{m2['id']}").status_code == 200
    listing = client.get(f"/attachments?sha256={sha(PNG)}").json()
    assert listing["items"][0]["reference_count"] == 2


def test_rejection_checks_order_empty_duplicate_missing_mismatch(client):
    """同一请求可命中多个问题时，校验顺序固定：空→重复→缺失→指纹。"""
    m = post(client, b"Subject: t\r\n\r\nx\r\n")
    # 重复优先于缺失
    r = unarchive(
        client,
        [
            {"id": 999, "fingerprint": "a" * 64},
            {"id": 999, "fingerprint": "a" * 64},
        ],
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNARCHIVE_DUPLICATE_IDS"
    # 缺失优先于指纹不符
    r = unarchive(
        client,
        [
            {"id": 999, "fingerprint": "a" * 64},
            {"id": m["id"], "fingerprint": "b" * 64},
        ],
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "UNARCHIVE_MESSAGE_NOT_FOUND"


def test_validation_id_must_be_positive_int(client):
    r = client.post(
        "/messages/unarchive",
        json={"items": [{"id": 0, "fingerprint": "a" * 64}]},
    )
    assert r.status_code == 422


def test_fingerprint_must_be_nonempty_string(client):
    r = client.post(
        "/messages/unarchive", json={"items": [{"id": 1, "fingerprint": ""}]}
    )
    assert r.status_code == 422


def test_preview_rejects_invalid_batch_too(client):
    # 预览路径与执行共用校验
    r = unarchive(client, [], preview=True)
    assert r.status_code == 422
    m = post(client, b"Subject: t\r\n\r\nx\r\n")
    r = unarchive(
        client, [{"id": m["id"], "fingerprint": "bad"}], preview=True
    )
    assert r.status_code == 409
    assert client.get(f"/messages/{m['id']}").status_code == 200


# ---------------------------------------------------------------------------
# 失败回滚：任何中途失败，整批数据不变
# ---------------------------------------------------------------------------


def test_delete_failure_rolls_back_entire_batch(client):
    import os
    import sqlite3

    from fastapi.testclient import TestClient

    from app.main import app

    m1 = post(client, message_with_parts(bin_part(PNG), subject="m1"))
    m2 = post(client, message_with_parts(bin_part(PDF), subject="m2"))
    msg_count = client.get("/messages").json()["total"]

    # 直接在库上安装一个触发器：删除 messages 行时抛错，模拟事务中途失败。
    db_path = os.environ["MIME_DB_PATH"]
    aux = sqlite3.connect(db_path)
    try:
        aux.execute(
            "CREATE TRIGGER fail_delete_messages BEFORE DELETE ON messages "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
        aux.commit()
    finally:
        aux.close()

    # raise_server_exceptions=False：观察真实的 500 响应而非测试端重抛。
    quiet = TestClient(app, raise_server_exceptions=False)
    r = quiet.post(
        "/messages/unarchive",
        json={
            "items": [
                {"id": m1["id"], "fingerprint": m1["fingerprint"]},
                {"id": m2["id"], "fingerprint": m2["fingerprint"]},
            ]
        },
    )
    assert r.status_code == 500, r.text

    # 整批回滚：两封邮件、节点、引用、附件实体全部保留
    assert client.get("/messages").json()["total"] == msg_count
    assert client.get(f"/messages/{m1['id']}").status_code == 200
    assert client.get(f"/messages/{m2['id']}").status_code == 200
    for digest in (sha(PNG), sha(PDF)):
        assert client.get(f"/attachments/{digest}/content").status_code == 200
        listing = client.get(f"/attachments?sha256={digest}").json()
        assert listing["items"][0]["reference_count"] == 1

    # 移除触发器后重试，整批成功
    aux = sqlite3.connect(db_path)
    try:
        aux.execute("DROP TRIGGER fail_delete_messages")
        aux.commit()
    finally:
        aux.close()
    r2 = unarchive(
        client,
        [
            {"id": m1["id"], "fingerprint": m1["fingerprint"]},
            {"id": m2["id"], "fingerprint": m2["fingerprint"]},
        ],
    )
    assert r2.status_code == 200
    assert client.get("/messages").json()["total"] == 0


# ---------------------------------------------------------------------------
# 撤销后既有功能不回归
# ---------------------------------------------------------------------------


def test_other_messages_query_thread_calendar_unaffected(client):
    m1 = post(
        client,
        b"From: alice@x\r\nSubject: keep\r\n\r\nbody\r\n",
    )
    m2 = post(
        client,
        b"Message-ID: <dead@x>\r\nSubject: gone\r\n\r\nx\r\n",
    )
    assert unarchive(
        client, [{"id": m2["id"], "fingerprint": m2["fingerprint"]}]
    ).status_code == 200

    # 列表/详情/树/日历等只读接口对保留邮件照常
    assert client.get(f"/messages/{m1['id']}").status_code == 200
    assert client.get(f"/messages/{m1['id']}/tree").status_code == 200
    events = client.get("/calendar/events")
    assert events.status_code == 200
    # 已撤销邮件不可再比对
    r = client.post("/messages/compare", json={"a": m1["id"], "b": m2["id"]})
    assert r.status_code == 404


def test_zip_download_still_works_for_retained_shared_blob(client):
    m1 = post(client, message_with_parts(bin_part(PNG, filename="a.png")))
    m2 = post(client, message_with_parts(bin_part(PNG, filename="b.png")))
    # 撤销 m1（PNG 因 m2 保留）
    assert unarchive(
        client, [{"id": m1["id"], "fingerprint": m1["fingerprint"]}]
    ).status_code == 200
    # m2 的 ZIP 仍能打包共享附件
    r = client.get(f"/messages/{m2['id']}/attachments.zip")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "application/zip"
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert "b.png" in zf.namelist()
        assert zf.read("b.png") == PNG
