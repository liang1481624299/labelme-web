"""labelme-web 账号与登录（SQLite + bcrypt）。

设计取舍（故意保持“简易”）：

* 账号存 SQLite（默认 ``var/labelme-web.db``，可用 ``LABELME_WEB_DB`` 覆盖）。
  放在 ``var/`` 而不是 ``data/``：``data/`` 是纯标注运行目录，删了会自动重建，
  账号不能跟着一起没。
* 密码只存 bcrypt 哈希串（cost 12），数据库里没有明文，日志里也不打密码。
* 登录态是随机 token，cookie 只带 token，库里存 token 的 sha256；7 天过期。
* 角色只有两级：``admin``（能进 /admin 管账号）与 ``annotator``（只能标注）。
* 第一个注册的账号自动成为管理员；之后注册的都是 annotator。
* 管理员可以关掉“允许注册”、禁用账号、改角色、重置密码、删账号。
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("LABELME_WEB_DB") or (BASE_DIR / "var" / "labelme-web.db"))
SESSION_COOKIE = "lmweb_session"
SESSION_DAYS = 7
BCRYPT_ROUNDS = 12
PASSWORD_MIN = 8
PASSWORD_MAX_BYTES = 72          # bcrypt 的硬上限，超过直接拒绝而不是悄悄截断
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fff]{2,32}$")
LOGIN_FAIL_WINDOW = 300          # 5 分钟内
LOGIN_FAIL_LIMIT = 6             # 失败 6 次锁 60 秒
LOGIN_LOCK_SECONDS = 60

router = APIRouter(prefix="/api/auth", tags=["auth"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"])

_LOCK = threading.RLock()
_FAILS: Dict[str, List[float]] = {}


# ------------------------------------------------------------------ database
def db_path() -> Path:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return DB_PATH


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'annotator',
                enabled       INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL,
                last_login_at TEXT
            );
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        conn.commit()


init_db()


def now_iso() -> str:
    return datetime.now().isoformat(" ", timespec="seconds")


def get_setting(key: str, default: str) -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, stored: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored.encode("ascii"))
    except (ValueError, TypeError):
        return False


def validate_password(password: Any) -> str:
    if not isinstance(password, str) or len(password) < PASSWORD_MIN:
        raise HTTPException(status_code=400, detail=f"密码至少 {PASSWORD_MIN} 位")
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise HTTPException(status_code=400, detail="密码过长（bcrypt 上限 72 字节）")
    return password


def validate_username(username: Any) -> str:
    if not isinstance(username, str) or not USERNAME_RE.match(username.strip()):
        raise HTTPException(status_code=400, detail="账号 2–32 位，可用中英文、数字、_ - .")
    return username.strip()


# ------------------------------------------------------------------ users
def user_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def count_users() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def find_user(username: str) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def find_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


# ------------------------------------------------------------------ token / cookie
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(" ", timespec="seconds")
    with connect() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now_iso(),))
        conn.execute(
            "INSERT INTO auth_sessions(token_hash, user_id, created_at, expires_at) VALUES(?,?,?,?)",
            (_token_hash(token), user_id, now_iso(), expires),
        )
        conn.commit()
    return token


def drop_session(token: Optional[str]) -> None:
    if not token:
        return
    with connect() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (_token_hash(token),))
        conn.commit()


def drop_user_sessions(user_id: int, keep: Optional[str] = None) -> None:
    with connect() as conn:
        if keep:
            conn.execute(
                "DELETE FROM auth_sessions WHERE user_id=? AND token_hash<>?",
                (user_id, _token_hash(keep)),
            )
        else:
            conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        conn.commit()


def user_from_request(request: Request) -> Optional[sqlite3.Row]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT u.* FROM auth_sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash=? AND s.expires_at > ?",
            (_token_hash(token), now_iso()),
        ).fetchone()
    if not row or not row["enabled"]:
        return None
    return row


def set_login_cookie(response: Response, request: Request, token: str) -> None:
    secure = (
        os.environ.get("LABELME_WEB_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}
        or request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


# ------------------------------------------------------------------ guards
def current_user(request: Request) -> Dict[str, Any]:
    row = user_from_request(request)
    if row is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user_row_to_dict(row)


def require_admin(user: Dict[str, Any] = None) -> Dict[str, Any]:
    """角色判断的纯函数版本，admin_guard 内部复用。"""
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


async def auth_guard(request: Request, call_next):
    """最外层兜底：除了登录/注册/健康检查，/api/* 一律要求登录。"""
    path = request.url.path
    public = path in {"/api/health", "/api/auth/login", "/api/auth/register"}
    if path.startswith("/api/") and not public:
        if user_from_request(request) is None:
            return JSONResponse({"detail": "请先登录"}, status_code=401)
    return await call_next(request)


def admin_guard(request: Request) -> Dict[str, Any]:
    row = user_from_request(request)
    if row is None:
        raise HTTPException(status_code=401, detail="请先登录")
    if not row["enabled"] or row["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user_row_to_dict(row)


# ------------------------------------------------------------------ throttling
def _fail_key(request: Request, username: str) -> str:
    ip = request.client.host if request.client else "-"
    return f"{ip}|{username.lower()}"


def check_throttle(request: Request, username: str) -> None:
    key = _fail_key(request, username)
    with _LOCK:
        stamps = [t for t in _FAILS.get(key, []) if time.time() - t < LOGIN_FAIL_WINDOW]
        _FAILS[key] = stamps
        if len(stamps) >= LOGIN_FAIL_LIMIT and time.time() - stamps[-1] < LOGIN_LOCK_SECONDS:
            raise HTTPException(status_code=429, detail="失败次数过多，请 1 分钟后再试")


def note_failure(request: Request, username: str) -> None:
    key = _fail_key(request, username)
    with _LOCK:
        _FAILS.setdefault(key, []).append(time.time())


def clear_failures(request: Request, username: str) -> None:
    with _LOCK:
        _FAILS.pop(_fail_key(request, username), None)


# ------------------------------------------------------------------ auth api
@router.get("/me")
def me(request: Request) -> Dict[str, Any]:
    return {"user": current_user(request)}


@router.post("/register")
def register(request: Request, payload: Dict[str, Any]) -> JSONResponse:
    username = validate_username(payload.get("username"))
    password = validate_password(payload.get("password"))
    if get_setting("allow_registration", "1") != "1" and count_users() > 0:
        raise HTTPException(status_code=403, detail="管理员已关闭注册")
    with _LOCK:
        if find_user(username):
            raise HTTPException(status_code=409, detail="该账号已存在")
        role = "admin" if count_users() == 0 else "annotator"
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, role, enabled, created_at, last_login_at) "
                "VALUES(?,?,?,1,?,?)",
                (username, hash_password(password), role, now_iso(), now_iso()),
            )
            user_id = cur.lastrowid
            conn.commit()
    token = issue_session(user_id)
    if role == "admin":
        try:  # 第一个管理员出现后，把老 data/<时间戳>/ 会话收进 用户名/默认工程
            from backend import workspace
            workspace.auto_migrate()
        except Exception:
            pass
    response = JSONResponse(
        {"user": user_row_to_dict(find_user_by_id(user_id)), "first_admin": role == "admin"},
        status_code=201,
    )
    set_login_cookie(response, request, token)
    return response


@router.post("/login")
def login(request: Request, payload: Dict[str, Any]) -> JSONResponse:
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        raise HTTPException(status_code=400, detail="请输入账号和密码")
    check_throttle(request, username)
    row = find_user(username)
    if row is None or not verify_password(password, row["password_hash"]):
        note_failure(request, username)
        raise HTTPException(status_code=401, detail="账号或密码不正确")
    if not row["enabled"]:
        raise HTTPException(status_code=403, detail="该账号已被禁用，请联系管理员")
    clear_failures(request, username)
    with connect() as conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_iso(), row["id"]))
        conn.commit()
    token = issue_session(row["id"])
    response = JSONResponse({"user": user_row_to_dict(find_user_by_id(row["id"]))})
    set_login_cookie(response, request, token)
    return response


@router.post("/logout")
def logout(request: Request) -> JSONResponse:
    drop_session(request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ------------------------------------------------------------------ admin api
@admin_router.get("/users")
def admin_list_users(admin: Dict[str, Any] = Depends(admin_guard)) -> Dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return {"users": [user_row_to_dict(r) for r in rows]}


@admin_router.post("/users", status_code=201)
def admin_create_user(
    payload: Dict[str, Any], admin: Dict[str, Any] = Depends(admin_guard)
) -> Dict[str, Any]:
    username = validate_username(payload.get("username"))
    password = validate_password(payload.get("password"))
    role = "admin" if payload.get("role") == "admin" else "annotator"
    with _LOCK:
        if find_user(username):
            raise HTTPException(status_code=409, detail="该账号已存在")
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, role, enabled, created_at) VALUES(?,?,?,1,?)",
                (username, hash_password(password), role, now_iso()),
            )
            conn.commit()
            new_id = cur.lastrowid
    return {"user": user_row_to_dict(find_user_by_id(new_id))}


BATCH_MAX = 200
BATCH_START_DEFAULT = 10000001


@admin_router.post("/users/batch", status_code=201)
def admin_batch_create_users(
    payload: Dict[str, Any], admin: Dict[str, Any] = Depends(admin_guard)
) -> Dict[str, Any]:
    """批量建号：账号名 = 8 位数字 ID，密码默认 = 同一个 ID。

    密码只在这一个响应里出现一次（库里存 bcrypt），前端负责导出一份清单。
    纯数字 8 位正好也满足「密码至少 8 位」，所以不用额外放宽策略。
    """
    try:
        count = int(payload.get("count") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="数量必须是整数")
    if count < 1 or count > BATCH_MAX:
        raise HTTPException(status_code=400, detail=f"一次只能建 1–{BATCH_MAX} 个")

    start_raw = payload.get("start")
    explicit_start: Optional[int] = None
    if start_raw not in (None, ""):
        try:
            explicit_start = int(start_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="起始 ID 必须是 8 位数字")
        if explicit_start < 10000000 or explicit_start > 99999999:
            raise HTTPException(status_code=400, detail="起始 ID 必须是 8 位数字（10000000–99999999）")

    created: List[Dict[str, Any]] = []
    with _LOCK:
        with connect() as conn:
            taken = {
                row["username"]
                for row in conn.execute("SELECT username FROM users").fetchall()
            }
            numeric = [int(name) for name in taken if name.isdigit() and len(name) == 8]
            cursor = explicit_start if explicit_start is not None else (
                max(numeric) + 1 if numeric else BATCH_START_DEFAULT
            )
            if cursor > 99999999:
                raise HTTPException(status_code=400, detail="8 位数字 ID 已经用完了")

            for _ in range(count):
                while cursor <= 99999999 and str(cursor) in taken:
                    cursor += 1
                if cursor > 99999999:
                    raise HTTPException(
                        status_code=400,
                        detail=f"8 位数字 ID 已用完，本次只建了 {len(created)} 个",
                    )
                username = str(cursor)
                password = username
                validate_username(username)
                validate_password(password)
                conn.execute(
                    "INSERT INTO users(username, password_hash, role, enabled, created_at) "
                    "VALUES(?,?,?,1,?)",
                    (username, hash_password(password), "annotator", now_iso()),
                )
                conn.commit()
                taken.add(username)
                created.append({"id": username, "username": username, "password": password})
                cursor += 1

    return {
        "created": created,
        "count": len(created),
        "first": created[0]["username"] if created else None,
        "last": created[-1]["username"] if created else None,
    }


def _drop_sessions_conn(conn: sqlite3.Connection, user_id: int, keep: Optional[str] = None) -> None:
    """在已有连接里清登录态，避免嵌套 connect() 把 SQLite 锁住。"""
    if keep:
        conn.execute(
            "DELETE FROM auth_sessions WHERE user_id=? AND token_hash<>?",
            (user_id, _token_hash(keep)),
        )
    else:
        conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))


def _other_active_admins(conn: sqlite3.Connection, exclude_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND enabled=1 AND id<>?",
        (exclude_id,),
    ).fetchone()["n"]


@admin_router.patch("/users/{user_id}")
def admin_update_user(
    request: Request, user_id: int, payload: Dict[str, Any],
    admin: Dict[str, Any] = Depends(admin_guard),
) -> Dict[str, Any]:
    row = find_user_by_id(user_id)
    if not row:
        raise HTTPException(status_code=404, detail="账号不存在")

    with connect() as conn:
        if "role" in payload:
            role = "admin" if payload.get("role") == "admin" else "annotator"
            if row["role"] == "admin" and role != "admin":
                if _other_active_admins(conn, user_id) == 0:
                    raise HTTPException(status_code=400, detail="至少要保留一个启用的管理员")
            conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
            if role != "admin":
                _drop_sessions_conn(conn, user_id)
        if "enabled" in payload:
            enabled = 1 if payload.get("enabled") else 0
            if not enabled:
                if user_id == admin["id"]:
                    raise HTTPException(status_code=400, detail="不能禁用自己")
                if row["role"] == "admin" and _other_active_admins(conn, user_id) == 0:
                    raise HTTPException(status_code=400, detail="至少要保留一个启用的管理员")
                _drop_sessions_conn(conn, user_id)
            conn.execute("UPDATE users SET enabled=? WHERE id=?", (enabled, user_id))
        if payload.get("password"):
            password = validate_password(payload.get("password"))
            conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (hash_password(password), user_id),
            )
            keep = request.cookies.get(SESSION_COOKIE) if user_id == admin["id"] else None
            _drop_sessions_conn(conn, user_id, keep=keep)
        conn.commit()

    return {"user": user_row_to_dict(find_user_by_id(user_id))}


@admin_router.delete("/users/{user_id}")
def admin_delete_user(
    request: Request, user_id: int, admin: Dict[str, Any] = Depends(admin_guard)
) -> Dict[str, Any]:
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="不能删除自己")
    row = find_user_by_id(user_id)
    if not row:
        raise HTTPException(status_code=404, detail="账号不存在")
    with connect() as conn:
        if row["role"] == "admin" and _other_active_admins(conn, user_id) == 0:
            raise HTTPException(status_code=400, detail="至少要保留一个启用的管理员")
        _drop_sessions_conn(conn, user_id)
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
    return {"deleted": user_id}


@admin_router.get("/settings")
def admin_settings(admin: Dict[str, Any] = Depends(admin_guard)) -> Dict[str, Any]:
    return {
        "allow_registration": get_setting("allow_registration", "1") == "1",
        "password_min": PASSWORD_MIN,
    }


@admin_router.put("/settings")
def admin_put_settings(
    payload: Dict[str, Any], admin: Dict[str, Any] = Depends(admin_guard)
) -> Dict[str, Any]:
    if "allow_registration" in payload:
        set_setting("allow_registration", "1" if payload.get("allow_registration") else "0")
    return {
        "allow_registration": get_setting("allow_registration", "1") == "1",
        "password_min": PASSWORD_MIN,
    }


@admin_router.get("/overview")
def admin_overview(admin: Dict[str, Any] = Depends(admin_guard)) -> Dict[str, Any]:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        enabled = conn.execute("SELECT COUNT(*) AS n FROM users WHERE enabled=1").fetchone()["n"]
        admins = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'").fetchone()["n"]
    return {"users": total, "enabled": enabled, "admins": admins}


# ------------------------------------------------------------------ pages
def login_page() -> FileResponse:
    return FileResponse(str(BASE_DIR / "frontend" / "login.html"), media_type="text/html")


def admin_page(user: Optional[sqlite3.Row]) -> Response:
    if user is None:
        return RedirectResponse("/login?next=/admin", status_code=302)
    if not user["enabled"] or user["role"] != "admin":
        return Response(
            "<!doctype html><meta charset=utf-8><title>403</title>"
            "<body style=\"font:14px/1.6 system-ui;background:#1e1e1e;color:#ddd;padding:48px\">"
            "<h2>403 · 需要管理员权限</h2><p>当前账号无权访问 /admin。</p>"
            "<p><a style=\"color:#4ea1d3\" href=\"/\">回到标注页</a></p>",
            status_code=403,
            media_type="text/html",
        )
    return FileResponse(str(BASE_DIR / "frontend" / "admin.html"), media_type="text/html")
