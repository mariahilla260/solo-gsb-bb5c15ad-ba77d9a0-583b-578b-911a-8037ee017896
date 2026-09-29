"""解析 / 批量撤销时抛出的错误。

解析错误都带有 :attr:`node_path`，指明邮件 MIME 树上的问题节点，
调用方据此向客户端返回“定位到节点”的 422 响应。批量撤销错误
（:class:`UnarchiveError`）带稳定错误码与结构化 ``detail``，
用于 404 / 409 / 422 等整批拒绝场景，响应体统一为 ``{"error": {...}}``。
"""

from __future__ import annotations


class MIMEParseError(Exception):
    """原始 RFC 5322 邮件无法被严格解析为 MIME 树。

    :param code: 稳定的机器可读错误码
    :param message: 人类可读的错误说明
    :param node_path: 出错节点路径。根节点为 ``""``，
        子节点为 ``"1"``、``"1/2"`` 这样的 1 基路径
    :param detail: 附加上下文（如字节偏移、参数名）
    """

    def __init__(
        self,
        code: str,
        message: str,
        node_path: str = "",
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.node_path = node_path
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "node_path": self.node_path,
                "detail": self.detail,
            }
        }


class UnarchiveError(Exception):
    """批量撤销请求被整批拒绝（空列表/重复 id/缺失/指纹不符等）。

    与 :class:`MIMEParseError` 同构，但不带 ``node_path``——撤销是
    跨邮件的批量操作，问题定位到请求条目（``detail`` 中的 id 列表）。

    :param status_code: HTTP 状态码（404 / 409 / 422）
    :param code: 稳定的机器可读错误码
    :param message: 人类可读的错误说明
    :param detail: 附加上下文（重复/缺失 id、指纹对比等）
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "detail": self.detail,
            }
        }
