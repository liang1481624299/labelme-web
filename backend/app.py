"""labelme-web backend.

Source deployment only (no docker). Serves the static frontend, the annotation
API, and the account/admin API. Disk layout (one user, one project, one
browser session per folder):

    data/<username>/<project>/20260101013030/114.jpg
    data/<username>/<project>/20260101013030/114.json

``114.json`` is written in labelme 7.11.0's own format, with ``imagePath``
pointing at the image file in the same folder, so the folder can be copied
straight into the desktop build and opened as an Annotation Folder.

会话 id（14 位时间戳）→ 真实路径 的映射存在 SQLite 索引里（见 workspace.py），
老布局 ``data/<时间戳>/`` 会在第一个管理员存在后自动迁移。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend import auth, workspace

MAX_UPLOAD_MB = int(os.environ.get("LABELME_WEB_MAX_UPLOAD_MB", "200"))
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


def _session_info(folder: Path) -> Dict[str, str]:
    return {"session": folder.name, "owner": folder.parent.parent.name, "project": folder.parent.name}


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


# ---------------------------------------------------------------- images
@app.post("/api/sessions/{session}/images")
async def upload_images(session: str, request: Request, files: List[UploadFile] = File(...)) -> Dict[str, Any]:
    """Store original images (jpg/png/...) next to their future .json."""
    folder = _folder(session, request)
    limit = MAX_UPLOAD_MB * 1024 * 1024
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
    info = _session_info(folder)
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
    return {"session": session, "images": items, **_session_info(folder)}


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
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
