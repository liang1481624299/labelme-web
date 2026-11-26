"""labelme-web backend.

Source deployment only (no docker). Serves the static frontend, the annotation
API, and the account/admin API. Disk layout is account → project → files:

    data/<username>/<project>/114.jpg
    data/<username>/<project>/114.json

``114.json`` is written in labelme 7.11.0's own format, with ``imagePath``
pointing at the image file in the same folder, so the folder can be copied
straight into the desktop build and opened as an Annotation Folder.

工程内只有一个固定工作空间（session id 形如 ``workspace-xxxxxxxxxxxx``），
API 仍保留 ``/api/sessions/{session}/...`` 这样前端不用重写；用户界面不再有会话层。
老布局 ``data/<时间戳>/`` 和工程内的旧会话目录会自动拍平到工程根目录。
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import activity, auth, settings_store, workspace

DEFAULT_MAX_UPLOAD_MB = int(os.environ.get("LABELME_WEB_MAX_UPLOAD_MB", "200"))
LABELME_VERSION = "7.11.0"
LABELS_MAX = 500
LABEL_MAXLEN = 120
NAME_RE = re.compile(r"^[^\x00/\\]{1,200}$")  # 文件名：不许带路径分隔符
IMAGE_SUFFIXES = workspace.IMAGE_SUFFIXES
DRAFT_DIRNAME = workspace.DRAFT_DIRNAME
DATA_DIR = workspace.DATA_DIR
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

workspace.init(auth.DB_PATH)
workspace.auto_migrate()
workspace.rebuild_index()

app = FastAPI(title="labelme-web", docs_url="/api/docs", openapi_url="/api/openapi.json")

# 账号：所有 /api/* （除登录/注册/健康检查）都要求登录，页面层再做一次拦截
app.middleware("http")(auth.auth_guard)
app.include_router(auth.router)
app.include_router(auth.admin_router)


# ---------------------------------------------------------------- helpers
def upload_max_mb() -> int:
    raw = settings_store.get("upload.max_mb", "LABELME_WEB_MAX_UPLOAD_MB", str(DEFAULT_MAX_UPLOAD_MB))
    try:
        value = int(raw)
    except ValueError:
        value = DEFAULT_MAX_UPLOAD_MB
    return max(1, min(value, 4096))


def safe_name(filename: str) -> str:
    name = os.path.basename(filename.replace("\\", "/"))
    name = name.replace("\x00", "").strip()
    if not name or len(name) > 200 or not NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="bad file name: %r" % filename)
    return name


def unique_target(folder: Path, name: str) -> Path:
    """Never overwrite an existing image: a.jpg, a-1.jpg, a-2.jpg ..."""
    target = folder / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    index = 1
    while True:
        candidate = folder / ("%s-%d%s" % (stem, index, suffix))
        if not candidate.exists():
            return candidate
        index += 1


def write_json_durable(path: Path, payload: Dict[str, Any]) -> None:
    """原子 + fsync 落盘。草稿要能扛住蓝屏/断电，不能只躺在系统缓存里。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:  # 目录项也刷一次，Linux 上断电后才不会丢 rename
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def draft_path(folder: Path, name: str) -> Path:
    return folder / DRAFT_DIRNAME / (name + ".json")


def mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _owner(request: Request, owner: Optional[str] = None) -> str:
    """普通账号只能用自己的用户名当目录；管理员可以指定别人。"""
    user = auth.current_user(request)
    if owner and owner.strip() and owner.strip() != user["username"]:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="只能访问自己的工程")
        try:
            return workspace.safe_component(owner, "用户名")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return user["username"]


def _project(project: Optional[str], owner: str) -> str:
    try:
        name = workspace.safe_component(project or workspace.DEFAULT_PROJECT, "工程名")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    workspace.ensure_project(owner, name)
    return name


def _folder(session: str, request: Optional[Request] = None) -> Path:
    """会话 id → 目录；普通账号拿不到别人的会话（按 404 处理，不泄露存在性）。"""
    try:
        owner, project, folder = workspace.session_path(session)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad session id")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="session not found")
    if request is not None:
        user = auth.current_user(request)
        if user["role"] != "admin" and owner != user["username"]:
            raise HTTPException(status_code=404, detail="session not found")
    return folder


def _session_info(session: str, folder: Path) -> Dict[str, str]:
    owner, project = _place(session)
    return {"session": session, "owner": owner, "project": project}


# ---------------------------------------------------------------- sessions
@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "labelme": LABELME_VERSION}


@app.get("/api/projects")
def list_projects(request: Request, owner: Optional[str] = None, scope: Optional[str] = None) -> Dict[str, Any]:
    """当前账号的工程列表；管理员可以用 owner=某人 或 scope=all 看全部。"""
    workspace.auto_migrate()
    user = auth.current_user(request)
    if scope == "all" and user["role"] == "admin":
        projects = workspace.list_projects(None)
        return {"projects": projects, "owner": "*"}
    target = _owner(request, owner)
    projects = workspace.list_projects(target)
    if not projects and target == user["username"]:
        workspace.ensure_project(target, workspace.DEFAULT_PROJECT)  # 新账号给一个默认工程
        projects = workspace.list_projects(target)
    return {"projects": projects, "owner": target}


@app.post("/api/projects")
def create_project(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    owner = _owner(request, payload.get("owner"))
    name = _project(payload.get("name"), owner)
    return {"project": {"owner": owner, "name": name}, "owner": owner}


@app.put("/api/projects")
def rename_project(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    """改名工程（管理员可指定 owner）。"""
    owner = _owner(request, payload.get("owner"))
    old = payload.get("name")
    new = payload.get("new_name") or payload.get("newName")
    if not old or not new:
        raise HTTPException(status_code=400, detail="需要 name 和 new_name")
    try:
        name = workspace.rename_project(owner, old, new)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="工程不存在")
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"project": {"owner": owner, "name": name}, "owner": owner}


@app.delete("/api/projects")
def delete_project(request: Request, payload: Optional[Dict[str, Any]] = None,
                   owner: Optional[str] = None, name: Optional[str] = None) -> Dict[str, Any]:
    """删除工程（连同全部会话、图片、标注）。"""
    payload = payload or {}
    target_owner = _owner(request, payload.get("owner") or owner)
    target_name = payload.get("name") or name
    if not target_name:
        raise HTTPException(status_code=400, detail="需要 name")
    try:
        dropped = workspace.delete_project(target_owner, target_name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="工程不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"owner": target_owner, "name": target_name, "sessions": dropped}


@app.get("/api/projects/detail")
def get_project_detail(request: Request, owner: Optional[str] = None,
                       name: Optional[str] = None) -> Dict[str, Any]:
    """工程概览（管理端用）：会话、图片、标注、标签集。"""
    target_owner = _owner(request, owner)
    if not name:
        raise HTTPException(status_code=400, detail="需要 name")
    try:
        return workspace.project_detail(target_owner, name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="工程不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/sessions")
def create_session(request: Request, owner: Optional[str] = None, project: Optional[str] = None) -> Dict[str, Any]:
    """在当前工程下新建 data/<用户名>/<工程>/<YYYYMMDDHHMMSS>/。"""
    workspace.auto_migrate()
    target_owner = _owner(request, owner)
    target_project = _project(project, target_owner)
    return workspace.create_session(target_owner, target_project)


@app.get("/api/sessions")
def list_sessions(
    request: Request, owner: Optional[str] = None, project: Optional[str] = None
) -> List[Dict[str, Any]]:
    """会话列表。返回**数组**（不是 {sessions:[...]}），和旧版接口保持一致。"""
    workspace.auto_migrate()
    user = auth.current_user(request)
    if owner:
        target_owner: Optional[str] = _owner(request, owner)
    elif user["role"] == "admin":
        target_owner = None
    else:
        target_owner = user["username"]
    target_project: Optional[str] = None
    if project:
        try:
            target_project = workspace.safe_component(project, "工程名")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    items = workspace.list_sessions(target_owner, target_project)
    return items


@app.get("/api/recent")
def recent_session(
    request: Request, owner: Optional[str] = None, project: Optional[str] = None
) -> Dict[str, Any]:
    workspace.auto_migrate()
    target_project: Optional[str] = None
    if project:
        try:
            target_project = workspace.safe_component(project, "工程名")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    found = workspace.recent(_owner(request, owner), target_project)
    if not found:
        raise HTTPException(status_code=404, detail="no session yet")
    return found


def _place(session: str) -> tuple:
    """会话 id → (owner, project)，只用于记录活动，不校验权限（调用方已校验过）。"""
    try:
        owner, project, _path = workspace.session_path(session)
        return owner, project
    except (ValueError, FileNotFoundError):
        return "", ""


def _actor(request: Request) -> str:
    try:
        return auth.current_user(request)["username"]
    except Exception:
        return ""


# ---------------------------------------------------------------- images
@app.post("/api/sessions/{session}/images")
async def upload_images(session: str, request: Request, files: List[UploadFile] = File(...)) -> Dict[str, Any]:
    """Store original images (jpg/png/...) next to their future .json."""
    folder = _folder(session, request)
    owner, project = _place(session)
    actor = _actor(request)
    limit = upload_max_mb() * 1024 * 1024
    saved: List[str] = []
    for upload in files:
        name = safe_name(upload.filename or "")
        if Path(name).suffix.lower() not in IMAGE_SUFFIXES:
            raise HTTPException(status_code=400, detail="unsupported image: %s" % name)
        data = await upload.read()
        if len(data) > limit:
            raise HTTPException(status_code=413, detail="image too large: %s" % name)
        target = unique_target(folder, name)
        target.write_bytes(data)
        saved.append(target.name)
        activity.record(actor, "upload", owner, project, session, target.name, len(data))
    info = _session_info(session, folder)
    return {"session": session, "saved": saved, **info}


@app.get("/api/sessions/{session}/images")
def list_images(session: str, request: Request) -> Dict[str, Any]:
    folder = _folder(session, request)
    items = [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "annotated": (folder / (path.name + ".json")).is_file(),
        }
        for path in workspace.image_files(folder)
    ]
    return {"session": session, "images": items, **_session_info(session, folder)}


@app.get("/api/sessions/{session}/raw/{name}")
def get_image(session: str, name: str, request: Request) -> FileResponse:
    folder = _folder(session, request)
    name = safe_name(name)
    path = folder / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    return FileResponse(str(path), headers={"Cache-Control": "no-store"})


@app.delete("/api/sessions/{session}/images/{name}")
def delete_image(session: str, name: str, request: Request) -> Dict[str, str]:
    """labelme's "Delete File": removes the image, its annotation and its draft."""
    folder = _folder(session, request)
    name = safe_name(name)
    for path in (folder / name, folder / (name + ".json"), draft_path(folder, name)):
        if path.is_file():
            path.unlink()
    owner, project = _place(session)
    activity.record(_actor(request), "delete", owner, project, session, name, 0, "删除图片及其标注")
    return {"deleted": name}


# ---------------------------------------------------------------- annotation json
@app.get("/api/sessions/{session}/annotations/{name}")
def get_annotation(session: str, name: str, request: Request) -> JSONResponse:
    folder = _folder(session, request)
    name = safe_name(name)
    path = folder / (name + ".json")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="annotation not found")
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@app.delete("/api/sessions/{session}/annotations/{name}")
def delete_annotation(session: str, name: str, request: Request) -> Dict[str, str]:
    """labelme's "Delete Annotation File": removes the JSON, keeps the image."""
    folder = _folder(session, request)
    name = safe_name(name)
    for path in (folder / (name + ".json"), draft_path(folder, name)):
        if path.is_file():
            path.unlink()
    return {"deleted": name}


@app.put("/api/sessions/{session}/annotations/{name}")
def put_annotation(
    session: str, name: str, request: Request, payload: Dict[str, Any] = Body(...)
) -> Dict[str, Any]:
    """Write the annotation file, keeping imagePath in sync with the folder."""
    folder = _folder(session, request)
    name = safe_name(name)
    image_path = folder / name
    if not image_path.is_file():
        raise HTTPException(status_code=404, detail="image not found")

    shapes = payload.get("shapes")
    if not isinstance(shapes, list):
        raise HTTPException(status_code=400, detail="shapes must be a list")

    normalized: Dict[str, Any] = {
        "version": str(payload.get("version") or LABELME_VERSION),
        "flags": payload.get("flags") if isinstance(payload.get("flags"), dict) else {},
        "shapes": shapes,
        "imagePath": name,
        "imageData": payload.get("imageData"),
        "imageHeight": int(payload.get("imageHeight") or 0),
        "imageWidth": int(payload.get("imageWidth") or 0),
    }
    for key, value in payload.items():
        if key not in normalized:
            normalized[key] = value
    write_json_durable(folder / (name + ".json"), normalized)
    owner, project = _place(session)
    activity.record(_actor(request), "annotate", owner, project, session, name + ".json",
                    (folder / (name + ".json")).stat().st_size if (folder / (name + ".json")).is_file() else 0,
                    "%d 个形状" % len(shapes))
    return {"saved": name + ".json", "shapes": len(shapes)}


# ---------------------------------------------------------------- crash drafts
@app.put("/api/sessions/{session}/drafts/{name}")
def put_draft(
    session: str, name: str, request: Request, payload: Dict[str, Any] = Body(...)
) -> Dict[str, Any]:
    """保存编辑中的草稿。

    浏览器崩溃 / 强杀 / 蓝屏时，localStorage 不一定能落盘（实测硬杀进程后
    localStorage 直接丢），所以草稿必须同时写进服务器：<会话目录>/.drafts/。
    文件名与图像同名，带 .json 后缀，正常保存标注后前端会删掉它。
    """
    folder = _folder(session, request)
    name = safe_name(name)
    shapes = payload.get("shapes")
    if not isinstance(shapes, list):
        raise HTTPException(status_code=400, detail="shapes must be a list")
    saved = {
        "name": name,
        "at": int(payload.get("at") or 0),
        "imageWidth": int(payload.get("imageWidth") or 0),
        "imageHeight": int(payload.get("imageHeight") or 0),
        "flags": payload.get("flags") if isinstance(payload.get("flags"), dict) else {},
        "shapes": shapes,
    }
    write_json_durable(draft_path(folder, name), saved)
    return {"saved": name, "shapes": len(shapes)}


@app.get("/api/sessions/{session}/drafts/{name}")
def get_draft(session: str, name: str, request: Request) -> Dict[str, Any]:
    """读草稿，并顺带给出“草稿是否比已保存的标注更新”，前端据此决定是否恢复。"""
    folder = _folder(session, request)
    name = safe_name(name)
    path = draft_path(folder, name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="draft not found")
    annotation_m = mtime(folder / (name + ".json"))
    draft_m = mtime(path)
    return {
        "draft": json.loads(path.read_text(encoding="utf-8")),
        "draftMtime": draft_m,
        "annotationMtime": annotation_m,
        "stale": annotation_m > draft_m,
    }


@app.delete("/api/sessions/{session}/drafts/{name}")
def delete_draft(session: str, name: str, request: Request) -> Dict[str, str]:
    folder = _folder(session, request)
    name = safe_name(name)
    path = draft_path(folder, name)
    if path.is_file():
        path.unlink()
    return {"deleted": name}


# ---------------------------------------------------------------- label set
def _normalize_labels(items: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for raw in items:
        label = str(raw).replace("\r", " ").replace("\n", " ").strip()
        if not label or label in seen:
            continue
        if len(label) > LABEL_MAXLEN:
            label = label[:LABEL_MAXLEN]
        seen.add(label)
        out.append(label)
        if len(out) >= LABELS_MAX:
            break
    return out


def read_labels(path: Path) -> List[str]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return _normalize_labels([line for line in text.splitlines()])


def write_labels(path: Path, labels: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(labels)
    if body:
        body += "\n"
    tmp = path.with_suffix(".txt.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


@app.get("/api/labels")
def get_labels(request: Request, owner: Optional[str] = None, project: Optional[str] = None) -> Dict[str, Any]:
    """工程级标签集，落在 data/<用户名>/<工程>/labels.txt（每行一个）。"""
    target_owner = _owner(request, owner)
    target_project = _project(project, target_owner)
    path = workspace.labels_path(target_owner, target_project)
    labels = read_labels(path)
    return {
        "labels": labels,
        "count": len(labels),
        "owner": target_owner,
        "project": target_project,
        "path": f"data/{target_owner}/{target_project}/labels.txt",
    }


@app.put("/api/labels")
def put_labels(
    request: Request, payload: Dict[str, Any], owner: Optional[str] = None, project: Optional[str] = None
) -> Dict[str, Any]:
    items = payload.get("labels")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="labels must be a list of strings")
    target_owner = _owner(request, owner)
    target_project = _project(project, target_owner)
    labels = _normalize_labels(items)
    write_labels(workspace.labels_path(target_owner, target_project), labels)
    return {
        "labels": labels,
        "count": len(labels),
        "owner": target_owner,
        "project": target_project,
        "path": f"data/{target_owner}/{target_project}/labels.txt",
    }


# ------------------------------------------------------------- project flags
@app.get("/api/flags")
def get_flags(request: Request, owner: Optional[str] = None, project: Optional[str] = None) -> Dict[str, Any]:
    """工程级 flag 定义，落在 data/<用户名>/<工程>/flags.txt（每行一个）。"""
    target_owner = _owner(request, owner)
    target_project = _project(project, target_owner)
    path = workspace.flags_path(target_owner, target_project)
    flags = read_labels(path)
    return {
        "flags": flags,
        "count": len(flags),
        "owner": target_owner,
        "project": target_project,
        "path": f"data/{target_owner}/{target_project}/flags.txt",
    }


@app.put("/api/flags")
def put_flags(
    request: Request, payload: Dict[str, Any], owner: Optional[str] = None, project: Optional[str] = None
) -> Dict[str, Any]:
    items = payload.get("flags")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="flags must be a list of strings")
    target_owner = _owner(request, owner)
    target_project = _project(project, target_owner)
    flags = _normalize_labels(items)
    write_labels(workspace.flags_path(target_owner, target_project), flags)
    return {
        "flags": flags,
        "count": len(flags),
        "owner": target_owner,
        "project": target_project,
        "path": f"data/{target_owner}/{target_project}/flags.txt",
    }


# ---------------------------------------------------------------- admin file browser
def _admin_ok(request: Request) -> Dict[str, Any]:
    return auth.admin_guard(request)


def _rel(value: str) -> Path:
    """把前端传来的相对路径解析成 DATA_DIR 下的真实路径，越界一律 400。"""
    root = DATA_DIR.resolve()
    if not value:
        return root
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="路径越界")
    return candidate


def _rel_str(path: Path) -> str:
    try:
        return path.resolve().relative_to(DATA_DIR.resolve()).as_posix()
    except ValueError:
        return ""


def _entry(path: Path, owner: str = "", project: str = "", session: str = "") -> Dict[str, Any]:
    is_dir = path.is_dir()
    info: Dict[str, Any] = {
        "name": path.name,
        "type": "dir" if is_dir else "file",
        "path": _rel_str(path),
        "size": 0 if is_dir else path.stat().st_size,
        "mtime": mtime(path) if is_dir else path.stat().st_mtime,
    }
    if is_dir:
        try:
            info["entries"] = sum(1 for child in path.iterdir() if not child.name.startswith("."))
        except OSError:
            info["entries"] = 0
    else:
        suffix = path.suffix.lower()
        info["kind"] = (
            "image" if suffix in IMAGE_SUFFIXES
            else "annotation" if suffix == ".json" and not path.name.endswith(".draft.json")
            else "labels" if path.name in {"labels.txt", "flags.txt"}
            else "other"
        )
    if owner:
        info["owner"] = owner
    if project:
        info["project"] = project
    if session:
        info["session"] = session
    return info


@app.get("/api/admin/files")
def admin_files(request: Request, owner: Optional[str] = None, project: Optional[str] = None,
                session: Optional[str] = None, path: Optional[str] = None) -> Dict[str, Any]:
    """管理员文件浏览器：账号 → 工程 → 文件（不再有会话层）。

    不传参数 → 列账号目录；给 owner → 列工程；给 owner+project → 列工程里的文件。
    """
    _admin_ok(request)
    workspace.auto_migrate()
    root = DATA_DIR.resolve()
    entries: List[Dict[str, Any]] = []
    place = {"owner": owner or "", "project": project or "", "session": ""}

    if owner and project:
        folder = _rel(Path(owner) / project)
        if not folder.is_dir():
            raise HTTPException(status_code=404, detail="工程不存在")
        who = activity.for_place(owner, project)
        for child in sorted(folder.iterdir(), key=lambda p: (p.is_dir(), p.name.lower())):
            if child.name.startswith(".") or child.is_dir():
                continue
            item = _entry(child, owner, project)
            meta = who.get(child.name)
            if meta:
                item["last_actor"] = meta["actor"]
                item["last_event"] = meta["event"]
                item["last_at"] = meta["at"]
            entries.append(item)
        place_label = f"{owner} / {project}"
    elif owner:
        folder = _rel(Path(owner))
        if not folder.is_dir():
            raise HTTPException(status_code=404, detail="账号目录不存在")
        for child in sorted(folder.iterdir(), key=lambda p: p.name):
            if not child.is_dir() or child.name.startswith("."):
                continue
            item = _entry(child, owner, child.name)
            item["projects"] = 1
            item["images"] = len(workspace.image_files(child))
            item["annotations"] = len(list(child.glob("*.json")))
            entries.append(item)
        place_label = owner
    else:
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if not child.is_dir() or child.name.startswith("."):
                continue
            projects = [p for p in child.iterdir() if p.is_dir() and not p.name.startswith(".")]
            item = _entry(child, child.name)
            item["projects"] = len(projects)
            item["images"] = sum(len(workspace.image_files(proj)) for proj in projects)
            item["annotations"] = sum(len(list(proj.glob("*.json"))) for proj in projects)
            entries.append(item)
        place_label = "data/"

    target = root / place["owner"] / place["project"]
    return {"place": place, "label": place_label, "entries": entries,
            "path": path or _rel_str(target)}


@app.get("/api/admin/files/raw")
def admin_file_raw(request: Request, path: str, download: int = 0) -> Response:
    """下载/预览 data 下的单个文件。只允许文件，不允许整个目录。"""
    _admin_ok(request)
    target = _rel(path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    name = target.name
    disposition = "attachment" if download else "inline"
    quoted = urllib.parse.quote(name)
    return FileResponse(
        str(target),
        filename=name if download else None,
        headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quoted}", "Cache-Control": "no-store"},
    )


@app.delete("/api/admin/files")
def admin_file_delete(request: Request, path: str) -> Dict[str, Any]:
    _admin_ok(request)
    target = _rel(path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    size = target.stat().st_size
    target.unlink()
    parts = _rel_str(target.parent).split("/")
    owner = parts[0] if len(parts) > 0 else ""
    project = parts[1] if len(parts) > 1 else ""
    session = workspace.workspace_id(owner, project) if owner and project else ""
    activity.record(_actor(request), "delete", owner, project, session, target.name, size, "管理员删除文件")
    return {"deleted": path, "size": size}


@app.get("/api/admin/uploaders")
def admin_uploaders(request: Request, limit: int = 200) -> Dict[str, Any]:
    """谁上传了什么：按人汇总 + 最近动作流水。"""
    _admin_ok(request)
    return {
        "stats": activity.stats(),
        "by_actor": activity.uploads_by_actor(),
        "recent": activity.recent(limit=limit),
    }


# ---------------------------------------------------------------- system settings
@app.get("/api/admin/system-config")
def get_system_config(request: Request) -> Dict[str, Any]:
    _admin_ok(request)
    return {
        "site_name": settings_store.get("site.name", "", "Labelme"),
        "upload_max_mb": upload_max_mb(),
        "allow_registration": settings_store.get("allow_registration", "LABELME_WEB_ALLOW_REGISTRATION", "1").lower()
        in {"1", "true", "yes", "on"},
        "image_suffixes": sorted(IMAGE_SUFFIXES),
        "labelme_version": LABELME_VERSION,
    }


@app.put("/api/admin/system-config")
def put_system_config(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    _admin_ok(request)
    saved: List[str] = []
    values: Dict[str, str] = {}
    if "site_name" in payload:
        name = str(payload.get("site_name") or "").strip()
        if len(name) > 40:
            raise HTTPException(status_code=400, detail="站点名称最多 40 个字")
        values["site.name"] = name or "Labelme"
        saved.append("site_name")
    if "upload_max_mb" in payload:
        try:
            limit = int(payload.get("upload_max_mb"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="上传上限要填整数 MB")
        if limit < 1 or limit > 4096:
            raise HTTPException(status_code=400, detail="上传上限需在 1–4096 MB 之间")
        values["upload.max_mb"] = str(limit)
        saved.append("upload_max_mb")
    if "allow_registration" in payload:
        values["allow_registration"] = "1" if payload.get("allow_registration") else "0"
        saved.append("allow_registration")
    if values:
        settings_store.set_many(values)
    return {**get_system_config(request), "saved": saved}


# ---------------------------------------------------------------- AI config
# 与桌面版「AI 模型」对话框保持同一套模型命名：SAM 家族为主，另留一个
# OpenAI 兼容接口的位置（只存配置，Web 端推理尚未接入）。
AI_MODEL_CATALOG: List[Dict[str, Any]] = [
    {"id": "sam2:base+", "name": "SAM 2 Base+", "caps": "点 · 框",
     "note": "点提示和框提示下速度与质量的最佳平衡。", "recommended": True,
     "license": "Apache License 2.0", "family": "sam"},
    {"id": "sam3:latest", "name": "SAM 3", "caps": "点 · 框 · 文本提示",
     "note": "最适合文本提示和查找多个对象。", "recommended": True,
     "license": "SAM License", "family": "sam"},
    {"id": "efficientsam:tiny", "name": "EfficientSAM Tiny", "caps": "点 · 框",
     "note": "最轻量，适合低配机器。", "license": "Apache License 2.0", "family": "sam"},
    {"id": "efficientsam:small", "name": "EfficientSAM Small", "caps": "点 · 框",
     "note": "轻量版，速度优先。", "license": "Apache License 2.0", "family": "sam"},
    {"id": "sam2:small", "name": "SAM 2 Small", "caps": "点 · 框",
     "note": "SAM 2 轻量模型。", "license": "Apache License 2.0", "family": "sam"},
    {"id": "sam2:large", "name": "SAM 2 Large", "caps": "点 · 框",
     "note": "SAM 2 大模型，质量更高、更吃显存。", "license": "Apache License 2.0", "family": "sam"},
    {"id": "sam:base", "name": "SAM Base", "caps": "点 · 框",
     "note": "初代 SAM Base。", "license": "Apache License 2.0", "family": "sam"},
    {"id": "sam:large", "name": "SAM Large", "caps": "点 · 框",
     "note": "初代 SAM Large。", "license": "Apache License 2.0", "family": "sam"},
    {"id": "sam:huge", "name": "SAM Huge", "caps": "点 · 框",
     "note": "初代 SAM 最大模型。", "license": "Apache License 2.0", "family": "sam"},
    {"id": "yoloworld:latest", "name": "YOLO-World", "caps": "文本提示",
     "note": "开放词表检测，适合按文字找目标。", "license": "GNU General Public License v3.0",
     "family": "detect"},
]
AI_DEFAULT_MODELS = [m["id"] for m in AI_MODEL_CATALOG]
AI_DEFAULT_MODEL = "sam2:base+"


def _ai_models() -> List[str]:
    return list(AI_DEFAULT_MODELS)


def _ai_provider() -> str:
    provider = settings_store.get("ai.provider", "", "sam").lower()
    return provider if provider in {"sam", "openai"} else "sam"


def _read_timeout(key: str, env_name: str = "", default: str = "30") -> int:
    try:
        return max(3, min(int(settings_store.get(key, env_name, default)), 300))
    except ValueError:
        return 30


def _ai_config(admin: bool = False) -> Dict[str, Any]:
    enabled = settings_store.get_bool("ai.enabled", "", False)
    provider = _ai_provider()
    # 旧库只有 ai.endpoint / ai.model / ai.api_key，读的时候平滑迁移到 SAM 段
    sam_endpoint = settings_store.get("ai.sam.endpoint", "", settings_store.get("ai.endpoint", "", ""))
    sam_model = settings_store.get("ai.sam.model", "", settings_store.get("ai.model", "", AI_DEFAULT_MODEL))
    sam_key = settings_store.get("ai.sam.api_key", "", settings_store.get("ai.api_key", "", ""))
    sam_timeout = _read_timeout("ai.sam.timeout", "", settings_store.get("ai.timeout", "", "30"))
    openai_base = settings_store.get("ai.openai.base_url", "LABELME_WEB_OPENAI_BASE_URL", "")
    openai_model = settings_store.get("ai.openai.model", "LABELME_WEB_OPENAI_MODEL", "")
    openai_key = settings_store.get("ai.openai.api_key", "LABELME_WEB_OPENAI_API_KEY", "")
    openai_timeout = _read_timeout("ai.openai.timeout", "", "30")

    sam: Dict[str, Any] = {
        "endpoint": sam_endpoint,
        "endpoint_configured": bool(sam_endpoint),
        "model": sam_model,
        "models": _ai_models(),
        "timeout": sam_timeout,
        "api_key_set": bool(sam_key),
    }
    openai: Dict[str, Any] = {
        "base_url": openai_base,
        "base_url_configured": bool(openai_base),
        "model": openai_model,
        "timeout": openai_timeout,
        "api_key_set": bool(openai_key),
        "reserved": True,
    }
    if admin:
        sam["api_key"] = settings_store.mask(sam_key)
        openai["api_key"] = settings_store.mask(openai_key)

    out: Dict[str, Any] = {
        "enabled": enabled,
        "provider": provider,
        "providers": ["sam", "openai"],
        "sam": sam,
        "openai": openai,
        "catalog": AI_MODEL_CATALOG,
        # 兼容旧前端/旧脚本的扁平字段（指向 SAM）
        "endpoint": sam_endpoint,
        "endpoint_configured": bool(sam_endpoint),
        "model": sam_model,
        "models": _ai_models(),
        "timeout": sam_timeout,
        "api_key_set": bool(sam_key),
    }
    if admin:
        out["api_key"] = settings_store.mask(sam_key)
    return out


def _check_endpoint(value: str, label: str) -> str:
    endpoint = str(value or "").strip().rstrip("/")
    if endpoint and not re.match(r"^https?://", endpoint):
        raise HTTPException(status_code=400, detail=f"{label}必须以 http:// 或 https:// 开头")
    return endpoint


def _check_timeout(value: Any) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="超时要填整数秒")
    if timeout < 3 or timeout > 300:
        raise HTTPException(status_code=400, detail="超时需在 3–300 秒之间")
    return timeout


@app.get("/api/admin/ai-config")
def get_ai_config(request: Request) -> Dict[str, Any]:
    _admin_ok(request)
    return _ai_config(admin=True)


@app.put("/api/admin/ai-config")
def put_ai_config(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """只存配置；真正的推理服务由部署方提供（见 README 的 /health 约定）。"""
    _admin_ok(request)
    values: Dict[str, str] = {}
    saved: List[str] = []

    if "enabled" in payload:
        values["ai.enabled"] = "1" if payload.get("enabled") else "0"
        saved.append("enabled")
    if "provider" in payload:
        provider = str(payload.get("provider") or "").strip().lower()
        if provider not in {"sam", "openai"}:
            raise HTTPException(status_code=400, detail="provider 只能是 sam 或 openai")
        values["ai.provider"] = provider
        saved.append("provider")

    sam = payload.get("sam") if isinstance(payload.get("sam"), dict) else {}
    openai = payload.get("openai") if isinstance(payload.get("openai"), dict) else {}
    # 兼容旧的扁平提交：endpoint/model/timeout/api_key 视作 SAM 段
    if not sam:
        sam = {k: payload.get(k) for k in ("endpoint", "model", "timeout", "api_key") if k in payload}

    if "endpoint" in sam:
        values["ai.sam.endpoint"] = _check_endpoint(sam.get("endpoint"), "SAM 服务地址")
        saved.append("sam.endpoint")
    if "model" in sam:
        model = str(sam.get("model") or "").strip()
        if not model:
            raise HTTPException(status_code=400, detail="SAM 默认模型不能为空")
        values["ai.sam.model"] = model[:80]
        saved.append("sam.model")
    if "timeout" in sam:
        values["ai.sam.timeout"] = str(_check_timeout(sam.get("timeout")))
        saved.append("sam.timeout")
    if "api_key" in sam:
        key = str(sam.get("api_key") or "").strip()
        if key:
            values["ai.sam.api_key"] = key
            saved.append("sam.api_key")

    if "base_url" in openai:
        values["ai.openai.base_url"] = _check_endpoint(openai.get("base_url"), "OpenAI 兼容地址")
        saved.append("openai.base_url")
    if "model" in openai:
        model = str(openai.get("model") or "").strip()
        if model:
            values["ai.openai.model"] = model[:80]
            saved.append("openai.model")
    if "timeout" in openai:
        values["ai.openai.timeout"] = str(_check_timeout(openai.get("timeout")))
        saved.append("openai.timeout")
    if "api_key" in openai:
        key = str(openai.get("api_key") or "").strip()
        if key:
            values["ai.openai.api_key"] = key
            saved.append("openai.api_key")

    # 扁平字段仍写一份旧键，保证旧脚本和已有备份能读
    if "endpoint" in sam:
        values["ai.endpoint"] = values["ai.sam.endpoint"]
    if "model" in sam:
        values["ai.model"] = values["ai.sam.model"]
    if "timeout" in sam:
        values["ai.timeout"] = values["ai.sam.timeout"]
    if "api_key" in sam and "ai.sam.api_key" in values:
        values["ai.api_key"] = values["ai.sam.api_key"]

    if values:
        settings_store.set_many(values)
    return {**_ai_config(admin=True), "saved": saved}


@app.delete("/api/admin/ai-config")
def clear_ai_config(request: Request) -> Dict[str, Any]:
    _admin_ok(request)
    settings_store.delete([
        "ai.enabled", "ai.provider", "ai.models",
        "ai.endpoint", "ai.model", "ai.api_key", "ai.timeout",
        "ai.sam.endpoint", "ai.sam.model", "ai.sam.api_key", "ai.sam.timeout",
        "ai.openai.base_url", "ai.openai.model", "ai.openai.api_key", "ai.openai.timeout",
    ])
    return {**_ai_config(admin=True), "saved": ["cleared"]}


@app.post("/api/admin/ai-config/test")
def test_ai_config(request: Request, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """探一下推理服务：
    - SAM：GET <endpoint>/health，2xx 即视为可达；
    - OpenAI 兼容：GET <base_url>/models，2xx 即视为可达。
    """
    _admin_ok(request)
    config = _ai_config()
    provider = str((payload or {}).get("provider") or config["provider"]).lower()
    if provider not in {"sam", "openai"}:
        provider = "sam"

    if provider == "openai":
        base = config["openai"]["base_url"]
        if not base:
            return {"ok": False, "provider": provider, "detail": "还没有配置 OpenAI 兼容接口地址"}
        base = base.rstrip("/")
        url = base if base.endswith("/models") else base + "/models"
        headers = {"Accept": "application/json"}
        key = settings_store.get("ai.openai.api_key", "LABELME_WEB_OPENAI_API_KEY", "")
        timeout = config["openai"]["timeout"]
    else:
        endpoint = config["sam"]["endpoint"]
        if not endpoint:
            return {"ok": False, "provider": provider, "detail": "还没有配置 SAM 推理服务地址"}
        url = endpoint if endpoint.endswith("/health") else endpoint + "/health"
        headers = {"Accept": "application/json"}
        key = settings_store.get("ai.sam.api_key", "", settings_store.get("ai.api_key", "", ""))
        timeout = config["sam"]["timeout"]

    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            body = resp.read(300).decode("utf-8", "replace")
            return {"ok": 200 <= resp.status < 300, "provider": provider, "status": resp.status,
                    "detail": body or "OK", "url": url}
    except Exception as exc:
        return {"ok": False, "provider": provider, "detail": str(exc)[:300], "url": url}


# ---------------------------------------------------------------- pages
@app.get("/", include_in_schema=False)
def page_index(request: Request) -> Response:
    """未登录不进标注页，直接送登录页。"""
    if auth.user_from_request(request) is None:
        target = "/login?next=/" + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse(target, status_code=302)
    return FileResponse(str(FRONTEND_DIR / "index.html"), media_type="text/html")


@app.get("/login", include_in_schema=False)
def page_login(request: Request) -> Response:
    if auth.user_from_request(request) is not None:
        nxt = request.query_params.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        return RedirectResponse(nxt, status_code=302)
    return FileResponse(str(FRONTEND_DIR / "login.html"), media_type="text/html")


@app.get("/admin", include_in_schema=False)
def page_admin(request: Request) -> Response:
    """管理面板只能手敲 /admin 进来：服务端校验角色，前端不放任何入口。"""
    row = auth.user_from_request(request)
    if row is None:
        return RedirectResponse("/login?next=/admin", status_code=302)
    return auth.admin_page(row)


# ---------------------------------------------------------------- static frontend
@app.exception_handler(StarletteHTTPException)
async def page_http_error(request: Request, exc: StarletteHTTPException) -> Response:
    """API 仍返回 JSON；页面路径 404 时给一张独立错误页，不再回退到首页。"""
    if (exc.status_code == 404 and not request.url.path.startswith("/api/")
            and not Path(request.url.path).suffix):
        page = FRONTEND_DIR / "404.html"
        if page.is_file():
            return FileResponse(str(page), status_code=404, media_type="text/html")
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
               include_in_schema=False)
def api_not_found(rest: str) -> Response:
    """未匹配的 /api/* 一律 JSON 404，不能被前端静态挂载吞成页面。"""
    return JSONResponse({"detail": "接口不存在"}, status_code=404)


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
