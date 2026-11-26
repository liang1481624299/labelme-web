"""文件活动记录：谁在什么时候上传了哪张图、谁保存了哪份标注。

只记录"人对文件做过的动作"，不改动文件本身的布局；表``file_events``挂在同一个
SQLite 里（默认 var/labelme-web.db，LABELME_WEB_DB 可覆盖），所以备份数据库时
这些痕迹跟着走。

管理端「工程管理 → 文件浏览器」靠它回答两个问题：
1. 这个会话里的某张图，是谁传的、什么时候传的；
2. 全站最近谁上传了什么。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("LABELME_WEB_DB") or (BASE_DIR / "var" / "labelme-web.db"))

EVENTS = ("upload", "annotate", "delete", "project")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_events (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                at       TEXT NOT NULL,
                actor    TEXT NOT NULL,
                event    TEXT NOT NULL,
                owner    TEXT NOT NULL DEFAULT '',
                project  TEXT NOT NULL DEFAULT '',
                session  TEXT NOT NULL DEFAULT '',
                name     TEXT NOT NULL DEFAULT '',
                size     INTEGER NOT NULL DEFAULT 0,
                detail   TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_at ON file_events(at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_place ON file_events(owner, project, session)")
        conn.commit()


init_db()


def now_iso() -> str:
    return datetime.now().isoformat(" ", timespec="seconds")


def record(actor: str, event: str, owner: str = "", project: str = "",
           session: str = "", name: str = "", size: int = 0, detail: str = "") -> None:
    if event not in EVENTS:
        return
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO file_events(at, actor, event, owner, project, session, name, size, detail) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (now_iso(), actor, event, owner or "", project or "", session or "", name or "", int(size or 0), detail or ""),
            )
            conn.commit()
    except sqlite3.Error:
        # 记流水失败不应该让上传/保存失败，静默跳过
        pass


def _row(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def recent(limit: int = 200, actor: Optional[str] = None, event: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM file_events"
    where: List[str] = []
    args: List[Any] = []
    if actor:
        where.append("actor = ?")
        args.append(actor)
    if event:
        where.append("event = ?")
        args.append(event)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 200), 1000)))
    with connect() as conn:
        return [_row(r) for r in conn.execute(sql, args).fetchall()]


def uploads_by_actor(limit: int = 50) -> List[Dict[str, Any]]:
    """按人汇总：上传了多少张、总共多少字节、最后活跃时间。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT actor, COUNT(*) AS files, SUM(size) AS bytes, MAX(at) AS last_at "
            "FROM file_events WHERE event='upload' GROUP BY actor ORDER BY files DESC LIMIT ?",
            (max(1, min(int(limit or 50), 500)),),
        ).fetchall()
    return [_row(r) for r in rows]


def for_place(owner: str, project: str, session: str = "") -> Dict[str, Dict[str, Any]]:
    """某个目录下每个文件名的最后一条记录，供浏览器显示「谁传的、什么时候」。"""
    sql = ("SELECT name, actor, event, at FROM file_events "
           "WHERE owner=? AND project=? AND session LIKE ? ORDER BY id ASC")
    args: List[Any] = [owner, project, (session + "%") if session else "%"]
    out: Dict[str, Dict[str, Any]] = {}
    with connect() as conn:
        for row in conn.execute(sql, args).fetchall():
            out[row["name"]] = {"actor": row["actor"], "event": row["event"], "at": row["at"]}
    return out


def stats() -> Dict[str, Any]:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM file_events").fetchone()["n"]
        uploads = conn.execute("SELECT COUNT(*) AS n FROM file_events WHERE event='upload'").fetchone()["n"]
        bytes_ = conn.execute("SELECT COALESCE(SUM(size),0) AS n FROM file_events WHERE event='upload'").fetchone()["n"]
        actors = conn.execute("SELECT COUNT(DISTINCT actor) AS n FROM file_events WHERE event='upload'").fetchone()["n"]
    return {"events": total, "uploads": uploads, "uploaded_bytes": bytes_, "uploaders": actors}
