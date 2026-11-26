"""工程（project）与标注会话的磁盘布局 + 索引。

新布局：``data/<用户名>/<工程名>/<YYYYMMDDHHMMSS>/`` — 用户名一层、工程一层、会话一层。
每个会话目录里仍然是「原图 + 同名 .json + .drafts/」，可以和桌面版 labelme 直接互拷。

会话 id 仍然只是 14 位时间戳（全局唯一，撞了就 ``_2``），真实路径由 SQLite 索引
（``session_index`` 表）维护，所以前端、草稿、恢复链路都不用改协议。

老布局 ``data/<时间戳>/`` 会在第一次有管理员时自动迁移到
``data/<管理员>/默认工程/<时间戳>/``，原来的 ``data/labels.txt`` 也搬进这个工程。
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LABELME_WEB_DATA") or (BASE_DIR / "data")).resolve()
LEGACY_LABELS = DATA_DIR / "labels.txt"
DEFAULT_PROJECT = "默认工程"
SESSION_RE = re.compile(r"^\d{14}(_\d+)?$")
COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fff ]{1,40}$")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
DRAFT_DIRNAME = ".drafts"

_LOCK = threading.RLock()
_DB: Optional[Path] = None


# ------------------------------------------------------------------ 初始化
def init(db_path: Path) -> None:
    global _DB
    _DB = Path(db_path)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                owner      TEXT NOT NULL,
                name       TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(owner, name)
            );
            CREATE TABLE IF NOT EXISTS session_index (
                id         TEXT PRIMARY KEY,
                owner      TEXT NOT NULL,
                project    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_session_owner ON session_index(owner, project);
            """
        )
        conn.commit()


def _conn() -> sqlite3.Connection:
    if _DB is None:
        raise RuntimeError("workspace.init() 未调用")
    _DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now().isoformat(" ", timespec="seconds")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def safe_component(value: str, field: str = "name") -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or ".." in text or not COMPONENT_RE.match(text):
        raise ValueError(f"{field} 只能 1–40 位，可用中英文、数字、空格、_ - .")
    return text


def project_dir(owner: str, project: str, create: bool = False) -> Path:
    owner = safe_component(owner, "用户名")
    project = safe_component(project, "工程名")
    path = DATA_DIR / owner / project
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


# ------------------------------------------------------------------ 工程
def ensure_project(owner: str, project: str) -> Path:
    path = project_dir(owner, project, create=True)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO projects(owner, name, created_at) VALUES(?,?,?) "
            "ON CONFLICT(owner, name) DO NOTHING",
            (safe_component(owner, "用户名"), safe_component(project, "工程名"), now_iso()),
        )
        conn.commit()
    return path


def list_projects(owner: Optional[str] = None) -> List[Dict[str, Any]]:
    """owner 为空表示管理员视角：所有用户的工程。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT owner, name, created_at FROM projects "
            + ("WHERE owner=? " if owner else "")
            + "ORDER BY owner, name",
            (owner,) if owner else (),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            sessions = conn.execute(
                "SELECT id FROM session_index WHERE owner=? AND project=?",
                (row["owner"], row["name"]),
            ).fetchall()
            live = [s["id"] for s in sessions if (DATA_DIR / row["owner"] / row["name"] / s["id"]).is_dir()]
            out.append(
                {
                    "owner": row["owner"],
                    "name": row["name"],
                    "created_at": row["created_at"],
                    "sessions": len(live),
                }
            )
    return out


def project_exists(owner: str, project: str) -> bool:
    return project_dir(owner, project).is_dir()


def rename_project(owner: str, old_name: str, new_name: str) -> str:
    """改工程名：搬目录 + 同步 projects / session_index 两张表。返回新名字。"""
    owner = safe_component(owner, "用户名")
    old_name = safe_component(old_name, "工程名")
    new_name = safe_component(new_name, "工程名")
    if old_name == DEFAULT_PROJECT and new_name == DEFAULT_PROJECT:
        return new_name
    src = project_dir(owner, old_name)
    dst = project_dir(owner, new_name)
    if not src.is_dir():
        raise FileNotFoundError("工程不存在")
    if dst.exists():
        raise FileExistsError("新工程名已被占用")
    with _LOCK:
        src.rename(dst)
        with _conn() as conn:
            conn.execute(
                "UPDATE projects SET name=? WHERE owner=? AND name=?", (new_name, owner, old_name)
            )
            conn.execute(
                "UPDATE session_index SET project=? WHERE owner=? AND project=?",
                (new_name, owner, old_name),
            )
            conn.commit()
    return new_name


def delete_project(owner: str, name: str) -> int:
    """删工程：整目录删掉（含所有会话和标注），返回删掉的会话数。"""
    owner = safe_component(owner, "用户名")
    name = safe_component(name, "工程名")
    path = project_dir(owner, name)
    if not path.is_dir():
        raise FileNotFoundError("工程不存在")
    with _LOCK:
        with _conn() as conn:
            sessions = conn.execute(
                "SELECT id FROM session_index WHERE owner=? AND project=?", (owner, name)
            ).fetchall()
            conn.execute("DELETE FROM session_index WHERE owner=? AND project=?", (owner, name))
            conn.execute("DELETE FROM projects WHERE owner=? AND name=?", (owner, name))
            conn.commit()
        shutil.rmtree(path, ignore_errors=True)
    return len(sessions)


def project_detail(owner: str, name: str) -> Dict[str, Any]:
    """工程概览：会话数、图片数、已标注数、最后活动时间。"""
    owner = safe_component(owner, "用户名")
    name = safe_component(name, "工程名")
    root = project_dir(owner, name)
    if not root.is_dir():
        raise FileNotFoundError("工程不存在")
    sessions: List[Dict[str, Any]] = []
    images = annotated = 0
    last_mtime = 0.0
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        imgs = image_files(folder)
        jsons = list(folder.glob("*.json"))
        images += len(imgs)
        annotated += len(jsons)
        mt = _mtime(folder)
        last_mtime = max(last_mtime, mt)
        sessions.append(
            {
                "session": folder.name,
                "images": len(imgs),
                "annotations": len(jsons),
                "mtime": mt,
            }
        )
    labels: List[str] = []
    lp = root / "labels.txt"
    if lp.is_file():
        labels = [ln.strip() for ln in lp.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return {
        "owner": owner,
        "name": name,
        "sessions": sessions,
        "session_count": len(sessions),
        "images": images,
        "annotations": annotated,
        "labels": labels,
        "mtime": last_mtime,
    }


# ------------------------------------------------------------------ 会话索引
def _session_row(session_id: str) -> Optional[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute("SELECT * FROM session_index WHERE id=?", (session_id,)).fetchone()


def _register(session_id: str, owner: str, project: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO session_index(id, owner, project, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET owner=excluded.owner, project=excluded.project",
            (session_id, owner, project, now_iso()),
        )
        conn.commit()


def session_path(session_id: str) -> Tuple[str, str, Path]:
    """会话 id → (owner, project, 目录)。索引丢了就全盘扫一遍再补。"""
    if not SESSION_RE.match(session_id):
        raise ValueError("bad session id")
    row = _session_row(session_id)
    if row:
        path = DATA_DIR / row["owner"] / row["project"] / session_id
        if path.is_dir():
            return row["owner"], row["project"], path
        with _conn() as conn:  # 目录被删了，索引作废
            conn.execute("DELETE FROM session_index WHERE id=?", (session_id,))
            conn.commit()
        raise FileNotFoundError(session_id)

    found = _scan_for(session_id)
    if found:
        owner, project, path = found
        _register(session_id, owner, project)
        return found
    raise FileNotFoundError(session_id)


def _scan_for(session_id: str) -> Optional[Tuple[str, str, Path]]:
    ensure_data_dir()
    for owner in DATA_DIR.iterdir():
        if not owner.is_dir():
            continue
        for project in owner.iterdir():
            if not project.is_dir():
                continue
            candidate = project / session_id
            if candidate.is_dir():
                return owner.name, project.name, candidate
    return None


def _unique_id(stamp: str, folder_root: Path) -> str:
    candidate = stamp
    index = 2
    while _session_row(candidate) is not None or (folder_root / candidate).exists():
        candidate = f"{stamp}_{index}"
        index += 1
    return candidate


def create_session(owner: str, project: str) -> Dict[str, Any]:
    ensure_project(owner, project)
    root = project_dir(owner, project)
    with _LOCK:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        sid = _unique_id(stamp, root)
        folder = root / sid
        folder.mkdir(parents=True)
        _register(sid, owner, project)
    return {"session": sid, "owner": owner, "project": project, "created": now_iso(), "path": f"{owner}/{project}/{sid}"}


def list_sessions(owner: Optional[str], project: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_data_dir()
    with _conn() as conn:
        if owner and project:
            rows = conn.execute(
                "SELECT * FROM session_index WHERE owner=? AND project=? ORDER BY id DESC",
                (owner, project),
            ).fetchall()
        elif owner:
            rows = conn.execute(
                "SELECT * FROM session_index WHERE owner=? ORDER BY id DESC", (owner,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM session_index ORDER BY id DESC").fetchall()

    items: List[Dict[str, Any]] = []
    for row in rows:
        folder = DATA_DIR / row["owner"] / row["project"] / row["id"]
        if not folder.is_dir():
            with _conn() as conn:
                conn.execute("DELETE FROM session_index WHERE id=?", (row["id"],))
                conn.commit()
            continue
        images = image_files(folder)
        annotated = sum(1 for p in images if (folder / (p.name + ".json")).is_file())
        items.append(
            {
                "session": row["id"],
                "owner": row["owner"],
                "project": row["project"],
                "images": len(images),
                "annotated": annotated,
                "created": datetime.fromtimestamp(folder.stat().st_mtime).isoformat(" ", timespec="seconds"),
                "path": f'{row["owner"]}/{row["project"]}/{row["id"]}',
            }
        )
    return items


def image_files(folder: Path) -> List[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


def labels_path(owner: str, project: str) -> Path:
    return project_dir(owner, project, create=True) / "labels.txt"


def flags_path(owner: str, project: str) -> Path:
    return project_dir(owner, project, create=True) / "flags.txt"


def session_max_mtime(owner: str, project: str, sid: str) -> Tuple[float, Optional[str], Optional[str]]:
    """返回 (最新 mtime, 图像名, 来源)，用来找“最近动过的会话/图”。"""
    folder = DATA_DIR / owner / project / sid
    latest = 0.0
    image: Optional[str] = None
    source: Optional[str] = None
    if not folder.is_dir():
        return 0.0, None, None
    for path in folder.glob("*.json"):
        them = path.stat().st_mtime
        if them >= latest:
            latest, image, source = them, path.name[:-5], "annotation"
    drafts = folder / DRAFT_DIRNAME
    if drafts.is_dir():
        for path in drafts.glob("*.json"):
            them = path.stat().st_mtime
            if them >= latest:
                latest, image, source = them, path.name[:-5], "draft"
    for path in image_files(folder):
        them = path.stat().st_mtime
        if them > latest:
            latest, image, source = them, path.name, "image"
    return latest, image, source


def recent(owner: Optional[str], project: Optional[str] = None) -> Optional[Dict[str, Any]]:
    best = None
    for item in list_sessions(owner, project):
        latest, image, source = session_max_mtime(item["owner"], item["project"], item["session"])
        if best is None or latest > best[0]:
            best = (latest, item, image, source)
    if best is None:
        return None
    latest, item, image, source = best
    draft = bool(image) and (DATA_DIR / item["owner"] / item["project"] / item["session"] / DRAFT_DIRNAME / (image + ".json")).is_file()
    return {
        "session": item["session"],
        "owner": item["owner"],
        "project": item["project"],
        "image": image,
        "source": source,
        "hasDraft": draft,
        "at": datetime.fromtimestamp(latest).isoformat(" ", timespec="seconds"),
    }


# ------------------------------------------------------------------ 迁移 / 重建
def first_admin_username() -> Optional[str]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT username FROM users WHERE role='admin' ORDER BY id LIMIT 1"
        ).fetchone()
    return row["username"] if row else None


def _legacy_dirs() -> List[Path]:
    ensure_data_dir()
    return [d for d in DATA_DIR.iterdir() if d.is_dir() and SESSION_RE.match(d.name)]


def auto_migrate() -> int:
    """老布局 data/<时间戳>/ → data/<管理员>/默认工程/<时间戳>/，返回迁移数量。"""
    legacy = _legacy_dirs()
    if not legacy:
        return 0
    admin = first_admin_username()
    if not admin:
        return 0  # 还没有管理员，先不动，等第一个管理员注册后再迁
    moved = 0
    with _LOCK:
        target_root = project_dir(admin, DEFAULT_PROJECT, create=True)
        ensure_project(admin, DEFAULT_PROJECT)
        for folder in legacy:
            target = target_root / folder.name
            if target.exists():
                continue
            shutil.move(str(folder), str(target))
            _register(folder.name, admin, DEFAULT_PROJECT)
            moved += 1
        if LEGACY_LABELS.is_file():
            target_labels = target_root / "labels.txt"
            if not target_labels.exists():
                shutil.move(str(LEGACY_LABELS), str(target_labels))
            else:
                LEGACY_LABELS.unlink()
    return moved


def rebuild_index() -> int:
    """把磁盘上存在但索引里没有的会话补进来（手工拷 data/、恢复备份后用）。"""
    ensure_data_dir()
    added = 0
    for owner in DATA_DIR.iterdir():
        if not owner.is_dir() or SESSION_RE.match(owner.name):
            continue
        for project in owner.iterdir():
            if not project.is_dir():
                continue
            with _conn() as conn:
                conn.execute(
                    "INSERT INTO projects(owner, name, created_at) VALUES(?,?,?) "
                    "ON CONFLICT(owner, name) DO NOTHING",
                    (owner.name, project.name, now_iso()),
                )
                conn.commit()
            for folder in project.iterdir():
                if not folder.is_dir() or not SESSION_RE.match(folder.name):
                    continue
                if _session_row(folder.name) is None:
                    _register(folder.name, owner.name, project.name)
                    added += 1
    return added
