"""运行期可改的外部认证配置（LDAP / SSO）。

为什么单独一层：这些配置既要能从 systemd 的 EnvironmentFile 给（运维习惯），
又要能让管理员在 /admin 页面里直接改（学校场景没人愿意为了改一个 base DN 去登服务器）。
所以读的时候 **数据库优先、环境变量兜底**，写的时候一律写数据库。

密钥（bind 密码、client secret、AI API key）以 Fernet 密文存库，密钥来自
`LABELME_WEB_SECRET_KEY` 或 `var/.secret.key`（0600）。数据库和密钥文件要分开保护；
备份时必须两个一起拷，只拿数据库无法还原明文。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("LABELME_WEB_DB") or (BASE_DIR / "var" / "labelme-web.db"))

# 只加密真正敏感的字段；其余开关、地址、标签保持明文，便于运维直接看库排查。
SECRET_KEYS = {
    "ldap.bind_password",
    "sso.client_secret",
    "ai.sam.api_key",
    "ai.openai.api_key",
    "ai.api_key",                 # 旧库扁平字段
}
ENC_PREFIX = "enc:v1:"
_KEY_CACHE: Optional[bytes] = None
logger = logging.getLogger(__name__)


def _load_key() -> bytes:
    """优先环境变量派生，其次 var/.secret.key；没有就生成并 0600 保存。

    密钥文件和数据库分开存：只拷数据库拿不到明文，但备份时必须两个一起拷。
    """
    global _KEY_CACHE
    if _KEY_CACHE is not None:
        return _KEY_CACHE
    raw = (os.environ.get("LABELME_WEB_SECRET_KEY") or "").strip()
    if raw:
        _KEY_CACHE = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
        return _KEY_CACHE
    path = Path(os.environ.get("LABELME_WEB_SECRET_KEY_FILE") or (DB_PATH.parent / ".secret.key"))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        key = path.read_bytes().strip()
    except FileNotFoundError:
        key = Fernet.generate_key()
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(key)
        except FileExistsError:
            key = path.read_bytes().strip()
    if not key:
        raise RuntimeError("密钥文件为空，无法解密敏感配置")
    _KEY_CACHE = key
    return key


def _fernet() -> Fernet:
    return Fernet(_load_key())


def _encrypt(value: str) -> str:
    if not value or value.startswith(ENC_PREFIX):
        return value
    return ENC_PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(ENC_PREFIX):
        return value  # 兼容升级前的明文，migrate_secrets() 会补加密
    try:
        return _fernet().decrypt(value[len(ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.warning("敏感配置解密失败，密钥文件或 LABELME_WEB_SECRET_KEY 可能已变更")
        return ""


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
            value = str(row["value"] or "").strip()
            return _decrypt(value) if key in SECRET_KEYS else value
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
            stored = _encrypt(str(value)) if key in SECRET_KEYS else str(value)
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, stored),
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


def migrate_secrets() -> None:
    """把升级前留下的明文密钥原地换成 enc:v1 密文；幂等。"""
    try:
        _load_key()  # 即使现在没有密文，也先确保密钥文件存在，备份时不会漏
    except RuntimeError:
        pass
    try:
        with _conn() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            updates = [
                (key, _encrypt(str(row["value"] or "")))
                for row in rows
                for key in [row["key"]]
                if key in SECRET_KEYS
                and row["value"]
                and not str(row["value"]).startswith(ENC_PREFIX)
            ]
            for key, value in updates:
                conn.execute("UPDATE settings SET value=? WHERE key=?", (value, key))
            if updates:
                conn.commit()
    except (sqlite3.Error, RuntimeError):
        # 密钥不可用时不要让整个服务起不来；读取侧会按空密钥处理。
        pass


migrate_secrets()
