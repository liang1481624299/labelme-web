"""labelme-web backend.

Source deployment only (no docker). Serves the static frontend and a small
JSON/HTTP API. Every browser session owns one timestamped folder under data/:

    data/20260101013030/114.jpg
    data/20260101013030/114.json

``114.json`` is written in labelme 7.11.0's own format, with ``imagePath``
pointing at the image file in the same folder, so the folder can be copied
straight into the desktop build and opened as an Annotation Folder.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
FRONTEND_DIR = BASE_DIR / "frontend"
MAX_UPLOAD_MB = int(os.environ.get("LABELME_WEB_MAX_UPLOAD_MB", "200"))
LABELME_VERSION = "7.11.0"
LABELS_FILE = DATA_DIR / "labels.txt"
LABELS_MAX = 500
LABEL_MAXLEN = 120

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
SESSION_RE = re.compile(r"^\d{14}(_\d+)?$")
NAME_RE = re.compile(r"^[\w\-. ()\u4e00-\u9fff]+$")
SESSION_LOCK = threading.Lock()
DRAFT_DIRNAME = ".drafts"   # 崩溃草稿：data/<session>/.drafts/<图名>.json，不进标注目录

DATA_DIR.mkdir(parents=True, exist_ok=True)


def ensure_data_dir() -> Path:
    """(Re)create data/ on demand.

    Someone may delete the whole data/ folder while the service is running;
    every endpoint that touches it repairs the folder first instead of 500ing.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


app = FastAPI(title="labelme-web", docs_url="/api/docs", openapi_url="/api/openapi.json")


# ---------------------------------------------------------------- helpers
def session_dir(session: str) -> Path:
    ensure_data_dir()
    if not SESSION_RE.match(session):
        raise HTTPException(status_code=400, detail="bad session id")
    path = DATA_DIR / session
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="session not found")
    return path


def safe_name(filename: str) -> str:
    name = os.path.basename(filename.replace("\\", "/"))
    name = name.replace("\x00", "").strip()
    if not name or len(name) > 200 or not NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="bad file name: %r" % filename)
    return name


def image_files(folder: Path) -> List[Path]:
    return sorted(
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )


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


# ---------------------------------------------------------------- sessions
@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "labelme": LABELME_VERSION}


@app.post("/api/sessions")
def create_session() -> Dict[str, Any]:
    """Create data/<YYYYMMDDHHMMSS>/ for one browser session."""
    ensure_data_dir()
    with SESSION_LOCK:
        while True:
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            folder = DATA_DIR / stamp
            if not folder.exists():
                folder.mkdir(parents=True)
                return {"session": stamp, "created": datetime.now().isoformat(" ")}
            index = 2
            while (DATA_DIR / ("%s_%d" % (stamp, index))).exists():
                index += 1
            folder = DATA_DIR / ("%s_%d" % (stamp, index))
            folder.mkdir(parents=True)
            return {"session": folder.name, "created": datetime.now().isoformat(" ")}


@app.get("/api/sessions")
def list_sessions() -> List[Dict[str, Any]]:
    ensure_data_dir()
    items: List[Dict[str, Any]] = []
    for folder in sorted(DATA_DIR.iterdir(), reverse=True):
        if not folder.is_dir() or not SESSION_RE.match(folder.name):
            continue
        images = image_files(folder)
        annotated = sum(1 for p in images if (folder / (p.name + ".json")).is_file())
        items.append(
            {
                "session": folder.name,
                "images": len(images),
                "annotated": annotated,
                "created": datetime.fromtimestamp(folder.stat().st_mtime)
                .isoformat(" ", timespec="seconds"),
            }
        )
    return items


# ---------------------------------------------------------------- images
@app.post("/api/sessions/{session}/images")
async def upload_images(
    session: str, files: List[UploadFile] = File(...)
) -> Dict[str, Any]:
    """Store original images (jpg/png/...) next to their future .json."""
    folder = session_dir(session)
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
    return {"session": session, "saved": saved}


@app.get("/api/sessions/{session}/images")
def list_images(session: str) -> Dict[str, Any]:
    folder = session_dir(session)
    items = [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "annotated": (folder / (path.name + ".json")).is_file(),
        }
        for path in image_files(folder)
    ]
    return {"session": session, "images": items}


@app.get("/api/sessions/{session}/raw/{name}")
def get_image(session: str, name: str) -> FileResponse:
    folder = session_dir(session)
    name = safe_name(name)
    path = folder / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    return FileResponse(str(path), headers={"Cache-Control": "no-store"})


@app.delete("/api/sessions/{session}/images/{name}")
def delete_image(session: str, name: str) -> Dict[str, str]:
    """labelme's "Delete File": removes the image, its annotation and its draft."""
    folder = session_dir(session)
    name = safe_name(name)
    for path in (folder / name, folder / (name + ".json"), draft_path(folder, name)):
        if path.is_file():
            path.unlink()
    return {"deleted": name}


# ---------------------------------------------------------------- annotation json
@app.get("/api/sessions/{session}/annotations/{name}")
def get_annotation(session: str, name: str) -> JSONResponse:
    folder = session_dir(session)
    name = safe_name(name)
    path = folder / (name + ".json")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="annotation not found")
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@app.put("/api/sessions/{session}/annotations/{name}")
def put_annotation(
    session: str, name: str, payload: Dict[str, Any] = Body(...)
) -> Dict[str, Any]:
    """Write the annotation file, keeping imagePath in sync with the folder."""
    folder = session_dir(session)
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
    session: str, name: str, payload: Dict[str, Any] = Body(...)
) -> Dict[str, Any]:
    """保存编辑中的草稿。

    浏览器崩溃 / 强杀 / 蓝屏时，localStorage 不一定能落盘（实测硬杀进程后
    localStorage 直接丢），所以草稿必须同时写进服务器：data/<session>/.drafts/。
    文件名与图像同名，带 .json 后缀，正常保存标注后前端会删掉它。
    """
    folder = session_dir(session)
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
def get_draft(session: str, name: str) -> Dict[str, Any]:
    """读草稿，并顺带给出“草稿是否比已保存的标注更新”，前端据此决定是否恢复。"""
    folder = session_dir(session)
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
def delete_draft(session: str, name: str) -> Dict[str, str]:
    folder = session_dir(session)
    name = safe_name(name)
    path = draft_path(folder, name)
    if path.is_file():
        path.unlink()
    return {"deleted": name}


@app.get("/api/recent")
def recent_session() -> Dict[str, Any]:
    """最近一次动过的会话与图像，供浏览器被强杀后（localStorage 丢了）重新接上。"""
    ensure_data_dir()
    best: Optional[tuple] = None
    for folder in DATA_DIR.iterdir():
        if not folder.is_dir() or not SESSION_RE.match(folder.name):
            continue
        latest = mtime(folder)
        image: Optional[str] = None
        group: Optional[str] = None
        for path in folder.glob("*.json"):
            if path.name.endswith(".json") and path.is_file():
                them = mtime(path)
                if them >= latest:
                    latest, image, group = them, path.name[:-5], "annotation"
        drafts = folder / DRAFT_DIRNAME
        if drafts.is_dir():
            for path in drafts.glob("*.json"):
                them = mtime(path)
                if them >= latest:
                    latest, image, group = them, path.name[:-5], "draft"
        for path in image_files(folder):
            them = mtime(path)
            if them > latest:
                latest, image, group = them, path.name, "image"
        if best is None or latest > best[0]:
            best = (latest, folder.name, image, group)

    if best is None:
        raise HTTPException(status_code=404, detail="no session yet")
    latest, session, image, group = best
    has_draft = bool(image) and draft_path(DATA_DIR / session, image).is_file()
    return {
        "session": session,
        "image": image,
        "source": group,
        "hasDraft": has_draft,
        "at": datetime.fromtimestamp(latest).isoformat(" ", timespec="seconds"),
    }


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


def read_labels() -> List[str]:
    ensure_data_dir()
    if not LABELS_FILE.is_file():
        return []
    text = LABELS_FILE.read_text(encoding="utf-8", errors="replace")
    return _normalize_labels([line for line in text.splitlines()])


def write_labels(labels: List[str]) -> None:
    ensure_data_dir()
    body = "\n".join(labels)
    if body:
        body += "\n"
    tmp = LABELS_FILE.with_suffix(".txt.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, LABELS_FILE)


@app.get("/api/labels")
def get_labels() -> Dict[str, Any]:
    """项目级标签集，落在 data/labels.txt（每行一个），桌面版可 --labels 直接复用。"""
    labels = read_labels()
    return {"labels": labels, "count": len(labels), "path": "data/labels.txt"}


@app.put("/api/labels")
def put_labels(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    items = payload.get("labels")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="labels must be a list of strings")
    labels = _normalize_labels(items)
    write_labels(labels)
    return {"labels": labels, "count": len(labels), "path": "data/labels.txt"}


# ---------------------------------------------------------------- static frontend
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
