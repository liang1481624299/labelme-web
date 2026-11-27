"""SQLite 连接小工具。

Python 的 ``with sqlite3.connect(...) as conn`` 只在退出时提交/回滚，**不会关闭连接**。
本项目的每个请求都会建连接，如果只 commit 不 close，连接和 WAL 句柄会一直累积；
上传几百张图片时每个文件记一条 activity 流水，很快就撞到进程的 open files 上限。

这里用 ``factory`` 让连接在 ``with`` 退出时额外执行 ``close()``，调用方仍可保持
原来的 ``with connect() as conn`` 写法。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional, Type


class ManagedConnection(sqlite3.Connection):
    """退出上下文时先 commit/rollback，再真正关闭连接。"""

    def __exit__(self, exc_type: Optional[Type[BaseException]],
                 exc: Optional[BaseException],
                 tb: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, tb))
        finally:
            self.close()


def connect(path: Any, timeout: float = 10) -> sqlite3.Connection:
    return sqlite3.connect(str(path), timeout=timeout, factory=ManagedConnection)
