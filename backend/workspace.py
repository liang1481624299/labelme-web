"""工程（project）与标注会话的磁盘布局 + 索引。

新布局：``data/<用户名>/<工程名>/<YYYYMMDDHHMMSS>/`` — 用户名一层、工程一层、会话一层。
每个会话目录里仍然是「原图 + 同名 .json + .drafts/」，可以和桌面版 labelme 直接互拷。

会话 id 仍然只是 14 位时间戳（全局唯一，撞了就 ``_2``），真实路径由 SQLite 索引
（``session_index`` 表）维护，所以前端、草稿、恢复链路都不用改协议。

老布局 ``data/<时间戳>/`` 会在第一次有管理员时自动迁移到
``data/<管理员>/默认工程/<时间戳>/``，原来的 ``data/labels.txt`` 也搬进这个工程。
"""

from __future__ import annotations

import hashlib
import json
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
SESSION_RE = re.compile(r"^(?:\d{14}(?:_\d+)?|workspace-[0-9a-f]{12})$")
WORKSPACE_PREFIX = "workspace-"
COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fff ]{1,40}$")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
DRAFT_DIRNAME = ".drafts"

_LOCK = threading.RLock()
_DB: Optional[Path] = None


def workspace_id(owner: str, project: str) -> str:
    """一个工程一个固定 id；前端仍拿它当 session 参数，但磁盘上就是工程根目录。"""
    digest = hashlib.sha1(f"{owner}\x00{project}".encode("utf-8")).hexdigest()[:12]
    return f"{WORKSPACE_PREFIX}{digest}"


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
    owner = safe_component(owner, "用户名")
    project = safe_component(project, "工程名")
    path = project_dir(owner, project, create=True)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO projects(owner, name, created_at) VALUES(?,?,?) "
            "ON CONFLICT(owner, name) DO NOTHING",
            (owner, project, now_iso()),
        )
        conn.commit()
    _register(workspace_id(owner, project), owner, project)
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
        live = (DATA_DIR / row["owner"] / row["name"]).is_dir()
        out.append(
            {
                "owner": row["owner"],
                "name": row["name"],
                "created_at": row["created_at"],
                "sessions": 1 if live else 0,
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
                "DELETE FROM session_index WHERE owner=? AND project=?",
                (owner, old_name),
            )
            conn.commit()
        _register(workspace_id(owner, new_name), owner, new_name)
    return new_name


def delete_project(owner: str, name: str) -> int:
    """删工程：整目录删掉（含全部图片和标注），返回 1 表示删掉一个工程空间。"""
    owner = safe_component(owner, "用户名")
    name = safe_component(name, "工程名")
    path = project_dir(owner, name)
    if not path.is_dir():
        raise FileNotFoundError("工程不存在")
    with _LOCK:
        with _conn() as conn:
            conn.execute("DELETE FROM session_index WHERE owner=? AND project=?", (owner, name))
            conn.execute("DELETE FROM projects WHERE owner=? AND name=?", (owner, name))
            conn.commit()
        shutil.rmtree(path, ignore_errors=True)
    return 1


def project_detail(owner: str, name: str) -> Dict[str, Any]:
    """工程概览：图片数、已标注数、最后活动时间（不再有会话层）。"""
    owner = safe_component(owner, "用户名")
    name = safe_component(name, "工程名")
    root = project_dir(owner, name)
    if not root.is_dir():
        raise FileNotFoundError("工程不存在")
    imgs = image_files(root)
    jsons = list(root.glob("*.json"))
    labels: List[str] = []
    lp = root / "labels.txt"
    if lp.is_file():
        labels = [ln.strip() for ln in lp.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return {
        "owner": owner,
        "name": name,
        "sessions": [],
        "session_count": 0,
        "images": len(imgs),
        "annotations": len(jsons),
        "labels": labels,
        "mtime": _mtime(root),
        "workspace": workspace_id(owner, name),
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
    """会话 id → (owner, project, 目录)。

    现在的 id 是工程级固定 workspace id，目录就是工程根目录；
    旧的 14 位时间戳会话仍能解析（迁移完成前保持兼容）。
    """
    if not SESSION_RE.match(session_id):
        raise ValueError("bad session id")
    row = _session_row(session_id)
    if row:
        root = DATA_DIR / row["owner"] / row["project"]
        if session_id.startswith(WORKSPACE_PREFIX):
            if root.is_dir():
                return row["owner"], row["project"], root
        else:
            path = root / session_id
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
    if session_id.startswith(WORKSPACE_PREFIX):
        for owner in DATA_DIR.iterdir():
            if not owner.is_dir() or owner.name.startswith("."):
                continue
            for project in owner.iterdir():
                if not project.is_dir() or project.name.startswith("."):
                    continue
                if workspace_id(owner.name, project.name) == session_id:
                    return owner.name, project.name, project
        return None
    for owner in DATA_DIR.iterdir():
        if not owner.is_dir() or owner.name.startswith("."):
            continue
        for project in owner.iterdir():
            if not project.is_dir() or project.name.startswith("."):
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
    """工程内只有一个固定工作空间；重复调用只会返回同一个 id。"""
    owner = safe_component(owner, "用户名")
    project = safe_component(project, "工程名")
    root = ensure_project(owner, project)
    sid = workspace_id(owner, project)
    _register(sid, owner, project)
    return {
        "session": sid,
        "owner": owner,
        "project": project,
        "created": datetime.fromtimestamp(root.stat().st_mtime).isoformat(" ", timespec="seconds"),
        "path": f"{owner}/{project}",
        "workspace": True,
    }


def _workspace_item(owner: str, project: str) -> Optional[Dict[str, Any]]:
    folder = DATA_DIR / owner / project
    if not folder.is_dir():
        return None
    images = image_files(folder)
    annotated = sum(1 for p in images if (folder / (p.name + ".json")).is_file())
    sid = workspace_id(owner, project)
    _register(sid, owner, project)
    return {
        "session": sid,
        "owner": owner,
        "project": project,
        "images": len(images),
        "annotated": annotated,
        "created": datetime.fromtimestamp(folder.stat().st_mtime).isoformat(" ", timespec="seconds"),
        "path": f"{owner}/{project}",
        "workspace": True,
    }


def list_sessions(owner: Optional[str], project: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_data_dir()
    if owner and project:
        item = _workspace_item(owner, project)
        return [item] if item else []
    pairs: List[Tuple[str, str]] = []
    if owner:
        base = DATA_DIR / owner
        if base.is_dir():
            for project_item in sorted(base.iterdir(), key=lambda p: p.name.lower()):
                if project_item.is_dir() and not project_item.name.startswith("."):
                    pairs.append((owner, project_item.name))
    else:
        base = DATA_DIR
        if base.is_dir():
            for owner_dir in sorted(base.iterdir(), key=lambda p: p.name.lower()):
                if not owner_dir.is_dir() or owner_dir.name.startswith(".") or SESSION_RE.match(owner_dir.name):
                    continue
                for project_item in sorted(owner_dir.iterdir(), key=lambda p: p.name.lower()):
                    if project_item.is_dir() and not project_item.name.startswith("."):
                        pairs.append((owner_dir.name, project_item.name))
    items: List[Dict[str, Any]] = []
    for pair_owner, pair_project in pairs:
        item = _workspace_item(pair_owner, pair_project)
        if item:
            items.append(item)
    return items


def image_files(folder: Path) -> List[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


def labels_path(owner: str, project: str) -> Path:
    return project_dir(owner, project, create=True) / "labels.txt"


def flags_path(owner: str, project: str) -> Path:
    return project_dir(owner, project, create=True) / "flags.txt"


def session_max_mtime(owner: str, project: str, sid: str) -> Tuple[float, Optional[str], Optional[str]]:
    """返回 (最新 mtime, 图像名, 来源)，用来找“最近动过的工程/图”。"""
    folder = project_dir(owner, project) if sid.startswith(WORKSPACE_PREFIX) else DATA_DIR / owner / project / sid
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
    root = project_dir(item["owner"], item["project"])
    draft = bool(image) and (root / DRAFT_DIRNAME / (image + ".json")).is_file()
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
    """老布局 data/<时间戳>/ → 管理员默认工程根目录；顺便把工程内的旧会话目录拍平。"""
    legacy = _legacy_dirs()
    moved = 0
    admin = first_admin_username()
    if legacy and admin:
        with _LOCK:
            target_root = project_dir(admin, DEFAULT_PROJECT, create=True)
            ensure_project(admin, DEFAULT_PROJECT)
            for folder in legacy:
                moved += _move_into_root(folder, target_root, folder.name)
                shutil.rmtree(folder, ignore_errors=True)
            if LEGACY_LABELS.is_file():
                target_labels = target_root / "labels.txt"
                if not target_labels.exists():
                    shutil.move(str(LEGACY_LABELS), str(target_labels))
                else:
                    LEGACY_LABELS.unlink()
    flatten_all()
    return moved


def _renamed(name: str, tag: str) -> str:
    if name.endswith(".json"):
        stem = name[:-5]
        inner = Path(stem)
        return f"{inner.stem}-{tag}{inner.suffix}.json"
    p = Path(name)
    return f"{p.stem}-{tag}{p.suffix}"


def _move_into_root(src: Path, root: Path, tag: str) -> int:
    """把 src 里的文件搬到 root；重名时加 -<tag> 并把 JSON 的 imagePath 改成新图名。"""
    count = 0
    for child in sorted(src.iterdir()):
        if child.name in {".", ".."}:
            continue
        if child.is_dir() and child.name == DRAFT_DIRNAME:
            dest_dir = root / DRAFT_DIRNAME
            dest_dir.mkdir(exist_ok=True)
            for draft in child.iterdir():
                if draft.is_file():
                    target = dest_dir / draft.name
                    if target.exists():
                        target = dest_dir / _renamed(draft.name, tag)
                    if target.exists():
                        continue
                    shutil.move(str(draft), str(target))
                    count += 1
            continue
        if child.is_dir():
            # 会话里再套目录的情况极少，直接并到工程根下同名目录
            dest_dir = root / child.name
            dest_dir.mkdir(exist_ok=True)
            for grand in child.iterdir():
                if grand.is_file() and not (dest_dir / grand.name).exists():
                    shutil.move(str(grand), str(dest_dir / grand.name))
                    count += 1
            continue
        target = root / child.name
        renamed = False
        if target.exists():
            target = root / _renamed(child.name, tag)
            renamed = True
        if target.exists():
            continue
        shutil.move(str(child), str(target))
        if renamed and target.suffix.lower() == ".json":
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
                data["imagePath"] = target.name[:-5]
                target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        count += 1
    return count


def flatten_all() -> int:
    """把所有工程中的 14 位时间戳会话目录合并到工程根目录。"""
    ensure_data_dir()
    merged = 0
    for owner in DATA_DIR.iterdir():
        if not owner.is_dir() or owner.name.startswith(".") or SESSION_RE.match(owner.name):
            continue
        for project in owner.iterdir():
            if not project.is_dir() or project.name.startswith("."):
                continue
            merged += flatten_project(owner.name, project.name)
    return merged


def flatten_project(owner: str, project: str) -> int:
    root = project_dir(owner, project, create=True)
    merged = 0
    with _LOCK:
        for folder in list(root.iterdir()):
            if not folder.is_dir() or not re.match(r"^\d{14}(_\d+)?$", folder.name):
                continue
            merged += _move_into_root(folder, root, folder.name)
            shutil.rmtree(folder, ignore_errors=True)
        _register(workspace_id(owner, project), owner, project)
    return merged


def rebuild_index() -> int:
    """把磁盘上存在但索引里没有的工程补进来，并把旧会话目录拍平（手工拷 data/、恢复备份后用）。"""
    ensure_data_dir()
    added = 0
    for owner in DATA_DIR.iterdir():
        if not owner.is_dir() or owner.name.startswith(".") or SESSION_RE.match(owner.name):
            continue
        for project in owner.iterdir():
            if not project.is_dir() or project.name.startswith("."):
                continue
            with _conn() as conn:
                conn.execute(
                    "INSERT INTO projects(owner, name, created_at) VALUES(?,?,?) "
                    "ON CONFLICT(owner, name) DO NOTHING",
                    (owner.name, project.name, now_iso()),
                )
                conn.commit()
            flatten_project(owner.name, project.name)
            _register(workspace_id(owner.name, project.name), owner.name, project.name)
            added += 1
    return added
