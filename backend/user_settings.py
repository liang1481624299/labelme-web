"""用户级设置：每个账号一份，存在 ``var/labelme-web.db`` 的 ``user_settings`` 表里。

和 ``settings_store`` 的区别：那边是 LDAP / SSO 这类整站配置（管理员改），
这里是标注页“文件 → 设置”里的个人偏好，按用户名隔离，换浏览器也还在。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("LABELME_WEB_DB") or (BASE_DIR / "var" / "labelme-web.db"))

DEFAULT_SETTINGS: Dict[str, Any] = {
    "theme": "system",               # system / light / dark
    "language": "zh_CN",
    "auto_save": True,
    "store_image_data": False,       # 桌面版 Store image data
    "show_label_dialog": True,
    "shape_color_mode": "auto",      # auto / label / uniform
    "label_interval": 0,
    "uniform_color": "#d9d9d9",
    "fallback_color": "#8a8a8a",
    "keep_prev": False,
    "keep_zoom": False,
    "keep_bc": False,
    "labels_from_file": True,
    "labels_from_annotation": True,
    "show_labels": True,
    "auto_add_label": True,
    "fill_drawing": True,
    "ai_enabled": False,
    "ai_model": "",                  # 用户在“AI 模型”里选中的模型 id；空 = 用后端默认
}

BOOL_KEYS = {
    "auto_save", "store_image_data", "show_label_dialog", "keep_prev", "keep_zoom",
    "keep_bc", "labels_from_file", "labels_from_annotation", "show_labels",
    "auto_add_label", "fill_drawing", "ai_enabled",
}
COLOR_KEYS = {"uniform_color", "fallback_color"}
INT_RANGES = {"label_interval": (0, 100)}
ENUMS = {
    "theme": {"system", "light", "dark"},
    "language": {"zh_CN"},
    "shape_color_mode": {"auto", "label", "uniform"},
}
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_settings ("
        "username TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.commit()
    return conn


def _clean(payload: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    """只接受已知键，类型不对的直接忽略，避免把任意 JSON 写进库。"""
    result = dict(current)
    if not isinstance(payload, dict):
        return result
    for key, value in payload.items():
        if key not in DEFAULT_SETTINGS:
            continue
        if key in BOOL_KEYS:
            if isinstance(value, bool):
                result[key] = value
        elif key in COLOR_KEYS:
            if isinstance(value, str) and COLOR_RE.match(value):
                result[key] = value.lower()
        elif key in INT_RANGES:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                lo, hi = INT_RANGES[key]
                result[key] = max(lo, min(hi, int(value)))
        elif key in ENUMS:
            if isinstance(value, str) and value in ENUMS[key]:
                result[key] = value
        elif key == "ai_model":
            if isinstance(value, str):
                result[key] = value.strip()[:80]
    return result


def get(username: str) -> Dict[str, Any]:
    data = dict(DEFAULT_SETTINGS)
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT data FROM user_settings WHERE username=?", (username,)
            ).fetchone()
        if row is not None:
            stored = json.loads(row["data"])
            if isinstance(stored, dict):
                data = _clean(stored, data)
    except (sqlite3.Error, json.JSONDecodeError, TypeError):
        pass
    return data


def save(username: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    current = get(username)
    data = _clean(payload, current)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as conn:
        conn.execute(
            "INSERT INTO user_settings(username, data, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (username, json.dumps(data, ensure_ascii=False, separators=(",", ":")), now),
        )
        conn.commit()
    return data
