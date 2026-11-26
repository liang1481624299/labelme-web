"""运行期可改的外部认证配置（LDAP / SSO）。

为什么单独一层：这些配置既要能从 systemd 的 EnvironmentFile 给（运维习惯），
又要能让管理员在 /admin 页面里直接改（学校场景没人愿意为了改一个 base DN 去登服务器）。
所以读的时候 **数据库优先、环境变量兜底**，写的时候一律写数据库。

密钥（bind 密码、client secret）也存在这个表里，明文。前提是这台机器的
`var/labelme-web.db` 本身受操作系统权限保护 —— 这点和现有的 bcrypt 账号库是同一个信任边界。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("LABELME_WEB_DB") or (BASE_DIR / "var" / "labelme-web.db"))


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    return conn


def get(key: str, env_name: str = "", default: str = "") -> str:
    """数据库里有就用数据库的，否则退回环境变量，再否则用 default。"""
    try:
        with _conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is not None:
            return str(row["value"] or "").strip()
    except sqlite3.Error:
        pass
    if env_name:
        return (os.environ.get(env_name) or default).strip()
    return default


def get_bool(key: str, env_name: str = "", default: bool = False) -> bool:
    raw = get(key, env_name, "1" if default else "0").lower()
    return raw in {"1", "true", "yes", "on"}


def set_many(values: Dict[str, str]) -> None:
    with _conn() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
        conn.commit()


def delete(keys: list[str]) -> None:
    with _conn() as conn:
        for key in keys:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
        conn.commit()


def mask(secret: Optional[str]) -> str:
    """返回给前端看的值：只留长度和最末 4 位，别把密码整串吐到浏览器里。"""
    if not secret:
        return ""
    text = str(secret)
    if len(text) <= 4:
        return "•" * len(text)
    return "•" * (len(text) - 4) + text[-4:]


def raw(key: str) -> Optional[str]:
    """不做环境变量回退，只看数据库里实际存了什么（给"是否被页面覆盖过"用）。"""
    try:
        with _conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row["value"])


def source_of(key: str, env_name: str) -> str:
    if raw(key) is not None:
        return "db"
    if env_name and os.environ.get(env_name):
        return "env"
    return "none"


def snapshot() -> Dict[str, Any]:
    try:
        with _conn() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    except sqlite3.Error:
        return {}
