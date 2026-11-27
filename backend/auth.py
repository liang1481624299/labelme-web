"""labelme-web 账号与登录（SQLite + bcrypt）。

设计取舍（故意保持“简易”）：

* 账号存 SQLite（默认 ``var/labelme-web.db``，可用 ``LABELME_WEB_DB`` 覆盖）。
  放在 ``var/`` 而不是 ``data/``：``data/`` 是纯标注运行目录，删了会自动重建，
  账号不能跟着一起没。
* 密码只存 bcrypt 哈希串（cost 12），数据库里没有明文，日志里也不打密码。
* 登录态是随机 token，cookie 只带 token，库里存 token 的 sha256；默认 30 分钟空闲超时、
  7 天绝对上限，会话里另存一枚 CSRF token。
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
from urllib.parse import quote, urlsplit

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse

from backend import ldap_auth, settings_store, sso_auth

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("LABELME_WEB_DB") or (BASE_DIR / "var" / "labelme-web.db"))
SESSION_COOKIE = "lmweb_session"


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.environ.get(name, "") or default)))
    except ValueError:
        return default


SESSION_DAYS = _env_int("LABELME_WEB_SESSION_DAYS", 7, 1, 30)
SESSION_IDLE_MINUTES = _env_int("LABELME_WEB_SESSION_IDLE_MINUTES", 30, 5, 1440)
SESSION_IDLE_SECONDS = SESSION_IDLE_MINUTES * 60
SESSION_TOUCH_SECONDS = 60       # 活跃请求最多每分钟写一次 last_seen，避免每次 API 都写库
BCRYPT_ROUNDS = 12
PASSWORD_MIN = 8
PASSWORD_MAX_BYTES = 72          # bcrypt 的硬上限，超过直接拒绝而不是悄悄截断
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fff]{2,32}$")
LOGIN_FAIL_WINDOW = 300          # 5 分钟内
LOGIN_FAIL_LIMIT = 6             # 失败 6 次锁 60 秒
LOGIN_LOCK_SECONDS = 60
_FAIL_KEYS_MAX = 4096            # 防止攻击者用随机账号名把内存撑大

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
                expires_at TEXT NOT NULL,
                last_seen_at TEXT,
                csrf_token TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # 旧的 auth_sessions 只有 expires_at；补 last_seen_at 才能做空闲超时。
        session_cols = {r["name"] for r in conn.execute("PRAGMA table_info(auth_sessions)").fetchall()}
        if "last_seen_at" not in session_cols:
            conn.execute("ALTER TABLE auth_sessions ADD COLUMN last_seen_at TEXT")
        if "csrf_token" not in session_cols:
            conn.execute("ALTER TABLE auth_sessions ADD COLUMN csrf_token TEXT")
        conn.execute(
            "UPDATE auth_sessions SET last_seen_at=created_at "
            "WHERE last_seen_at IS NULL OR last_seen_at=''"
        )
        conn.execute(
            "UPDATE auth_sessions SET csrf_token=lower(hex(randomblob(24))) "
            "WHERE csrf_token IS NULL OR csrf_token=''"
        )
        # LDAP 接入层：影子账号要记来源和外部 ID（老库用 ALTER 补齐，幂等）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "source" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN source TEXT NOT NULL DEFAULT 'local'")
        if "external_id" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN external_id TEXT")
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
    keys = row.keys()
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
        "source": row["source"] if "source" in keys else "local",
        "external_id": row["external_id"] if "external_id" in keys else None,
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


# ------------------------------------------------------------------ external shadow users
def external_user(username: str, external_id: str, source: str, is_admin: bool) -> sqlite3.Row:
    """外部身份（LDAP / SSO）认证通过后落一条本地影子账号。

    * 账号名必须符合本站命名规则（它同时是 data/ 下的目录名）
    * 已存在的同名本地账号不覆盖密码，只按组映射补管理员权限（不降级，避免误锁）
    """
    if not USERNAME_RE.match(username or ""):
        raise HTTPException(status_code=403, detail="该外部账号名不符合本站命名规则（2–32 位，中英文/数字/_ - .）")
    with _LOCK:
        row = find_user(username)
        if row is None:
            role = "admin" if (is_admin or count_users() == 0) else "annotator"
            with connect() as conn:
                cur = conn.execute(
                    "INSERT INTO users(username, password_hash, role, enabled, created_at, last_login_at, source, external_id) "
                    "VALUES(?,?,?,1,?,?,?,?)",
                    (username, "!" + source, role, now_iso(), now_iso(), source, external_id),
                )
                conn.commit()
            return find_user_by_id(cur.lastrowid)
        if is_admin and row["role"] != "admin":
            with connect() as conn:
                conn.execute("UPDATE users SET role='admin' WHERE id=?", (row["id"],))
                conn.commit()
            row = find_user_by_id(row["id"])
        return row


def ldap_shadow_user(lu: "ldap_auth.LdapUser") -> sqlite3.Row:
    return external_user(lu.username, lu.dn, "ldap", lu.is_admin)


def sso_shadow_user(su: "sso_auth.SsoUser") -> sqlite3.Row:
    return external_user(su.username, su.sub, "sso", su.is_admin)


# ------------------------------------------------------------------ token / cookie
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = datetime.now()
    now_text = now.isoformat(" ", timespec="seconds")
    expires = (now + timedelta(days=SESSION_DAYS)).isoformat(" ", timespec="seconds")
    idle_cutoff = (now - timedelta(seconds=SESSION_IDLE_SECONDS)).isoformat(" ", timespec="seconds")
    with connect() as conn:
        # 顺手清掉绝对过期和长时间无操作的会话，避免表无限增长。
        conn.execute(
            "DELETE FROM auth_sessions WHERE expires_at < ? OR last_seen_at < ?",
            (now_text, idle_cutoff),
        )
        conn.execute(
            "INSERT INTO auth_sessions"
            "(token_hash, user_id, created_at, expires_at, last_seen_at, csrf_token) "
            "VALUES(?,?,?,?,?,?)",
            (_token_hash(token), user_id, now_text, expires, now_text, csrf),
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
    now_dt = datetime.now()
    now = now_dt.isoformat(" ", timespec="seconds")
    idle_cutoff = (now_dt - timedelta(seconds=SESSION_IDLE_SECONDS)).isoformat(" ", timespec="seconds")
    touch_cutoff = (now_dt - timedelta(seconds=SESSION_TOUCH_SECONDS)).isoformat(" ", timespec="seconds")
    with connect() as conn:
        row = conn.execute(
            "SELECT u.*, s.csrf_token FROM auth_sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash=? AND s.expires_at > ? AND s.last_seen_at >= ?",
            (_token_hash(token), now, idle_cutoff),
        ).fetchone()
        if row and not row["enabled"]:
            return None
        if not row:
            # 过期或空闲超时的 token 直接删掉，不留可被反复尝试的会话记录。
            conn.execute(
                "DELETE FROM auth_sessions WHERE token_hash=? AND (expires_at <= ? OR last_seen_at < ?)",
                (_token_hash(token), now, idle_cutoff),
            )
            conn.commit()
            return None
        # 滑动空闲窗口：活跃请求续期，但绝对 expires_at 不延长。
        conn.execute(
            "UPDATE auth_sessions SET last_seen_at=? WHERE token_hash=? AND last_seen_at < ?",
            (now, _token_hash(token), touch_cutoff),
        )
        conn.commit()
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


def _request_is_same_origin(request: Request) -> bool:
    """浏览器发起的写请求必须来自本站。

    Origin 缺失时（curl / 部分脚本）放行，因为此时不存在浏览器自动带 cookie
    的 CSRF 场景；但浏览器明确标了 cross-site / same-site 一律拒绝。
    """
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site in {"cross-site", "same-site"}:
        return False
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return True
    if origin == "null":
        return False
    host = (request.headers.get("host") or "").strip().lower()
    forwarded = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip().lower()
    allowed = {value for value in (host, forwarded) if value}
    return bool(allowed) and urlsplit(origin).netloc.lower() in allowed


async def auth_guard(request: Request, call_next):
    """最外层兜底：除登录/注册/健康检查，/api/* 一律要求登录；写接口再过 CSRF。"""
    path = request.url.path
    unsafe = request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    public = path in {
        "/api/health",
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/methods",
        "/api/auth/sso/start",
        "/api/auth/sso/callback",
    }
    if path.startswith("/api/") and unsafe and not _request_is_same_origin(request):
        return JSONResponse({"detail": "跨站请求已被拒绝"}, status_code=403)
    if path.startswith("/api/") and not public:
        row = user_from_request(request)
        if row is None:
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        if unsafe:
            sent = request.headers.get("x-csrf-token", "")
            expected = row["csrf_token"] or ""
            if not expected or not secrets.compare_digest(sent, expected):
                return JSONResponse({"detail": "CSRF 校验失败，请刷新页面后重试"}, status_code=403)
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
        if len(_FAILS) >= _FAIL_KEYS_MAX:
            cutoff = time.time() - LOGIN_FAIL_WINDOW
            stale = [k for k, stamps in _FAILS.items() if not stamps or stamps[-1] < cutoff]
            for stale_key in stale:
                _FAILS.pop(stale_key, None)
            if len(_FAILS) >= _FAIL_KEYS_MAX:
                # 仍然超限时丢掉最早的一半，宁可临时放宽限流也不让进程被撑爆。
                for old_key in list(_FAILS)[: max(1, len(_FAILS) // 2)]:
                    _FAILS.pop(old_key, None)
        _FAILS.setdefault(key, []).append(time.time())


def clear_failures(request: Request, username: str) -> None:
    with _LOCK:
        _FAILS.pop(_fail_key(request, username), None)


# ------------------------------------------------------------------ auth api
@router.get("/me")
def me(request: Request) -> Dict[str, Any]:
    row = user_from_request(request)
    if row is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return {"user": user_row_to_dict(row), "csrf_token": row["csrf_token"] or ""}


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
    via = "local"

    # 1) 本地密码优先（管理员应急入口，LDAP 挂了也能进）
    local_ok = row is not None and verify_password(password, row["password_hash"])
    if not local_ok:
        # 2) 本地不过就交给 LDAP（只有配了 LABELME_WEB_LDAP_URL 才生效）
        if not ldap_auth.enabled():
            note_failure(request, username)
            raise HTTPException(status_code=401, detail="账号或密码不正确")
        try:
            lu = ldap_auth.authenticate(username, password)
        except ldap_auth.LdapDenied:
            note_failure(request, username)
            raise HTTPException(status_code=401, detail="账号或密码不正确")
        except ldap_auth.LdapUnavailable as exc:
            # 连不上不算密码错，不累加失败次数，避免把本地账号也一起锁了
            raise HTTPException(
                status_code=503,
                detail=f"{ldap_auth.label()} 暂不可用（{exc}），请先用本地账号登录",
            )
        row = ldap_shadow_user(lu)
        via = "ldap"

    if row is None:
        note_failure(request, username)
        raise HTTPException(status_code=401, detail="账号或密码不正确")
    if not row["enabled"]:
        raise HTTPException(status_code=403, detail="该账号已被禁用，请联系管理员")
    clear_failures(request, username)
    with connect() as conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_iso(), row["id"]))
        conn.commit()
    token = issue_session(row["id"])
    response = JSONResponse({"user": user_row_to_dict(find_user_by_id(row["id"])), "via": via})
    set_login_cookie(response, request, token)
    return response


@router.get("/methods")
def auth_methods() -> Dict[str, Any]:
    """登录页用：告诉前端有哪几种登录方式，不泄露任何服务器地址或密钥。"""
    return {
        "password": True,
        "ldap": ldap_auth.public_config(),
        "sso": sso_auth.public_config(),
        "site_name": get_setting("site.name", "Labelme"),
        "upload_max_mb": get_setting("upload.max_mb", os.environ.get("LABELME_WEB_MAX_UPLOAD_MB", "200")),
    }


def _origin(request: Request) -> str:
    """反代后的真实 origin：优先 X-Forwarded-*，没有再退回请求本身。"""
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip() or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _safe_next(value: Optional[str]) -> str:
    """只接受本站绝对路径，挡住 //、反斜杠和编码后的协议相对跳转。"""
    if not value:
        return "/"
    text = str(value)
    if any(ord(ch) < 32 for ch in text):
        return "/"
    lowered = text.lower()
    if "\\" in text or "%2f" in lowered or "%5c" in lowered:
        return "/"
    if not text.startswith("/") or text.startswith("//"):
        return "/"
    try:
        parts = urlsplit(text)
    except ValueError:
        return "/"
    if parts.scheme or parts.netloc:
        return "/"
    return text


@router.get("/sso/start")
def sso_start(request: Request, next: Optional[str] = None) -> Response:
    """跳去 IdP 授权页。没启用 SSO 就当 404，不给探测面。"""
    if not sso_auth.enabled():
        raise HTTPException(status_code=404, detail="未启用 SSO")
    try:
        url = sso_auth.begin(_origin(request), _safe_next(next))
    except sso_auth.SsoUnavailable as exc:
        return RedirectResponse("/login?sso_error=" + quote(str(exc)), status_code=302)
    except sso_auth.SsoError as exc:
        return RedirectResponse("/login?sso_error=" + quote(str(exc)), status_code=302)
    return RedirectResponse(url, status_code=302)


@router.get("/sso/callback")
def sso_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None) -> Response:
    """IdP 回调：校验 token → 建/取影子账号 → 发本地会话 cookie → 回原页面。"""
    if not sso_auth.enabled():
        raise HTTPException(status_code=404, detail="未启用 SSO")
    try:
        user, stored = sso_auth.finish(code or "", state or "")
    except (sso_auth.SsoUnavailable, sso_auth.SsoError) as exc:
        return RedirectResponse("/login?sso_error=" + quote(str(exc)), status_code=302)
    try:
        row = sso_shadow_user(user)
    except HTTPException as exc:
        return RedirectResponse("/login?sso_error=" + quote(str(exc.detail)), status_code=302)
    if not row["enabled"]:
        return RedirectResponse("/login?sso_error=" + quote("该账号已被禁用，请联系管理员"), status_code=302)
    with connect() as conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_iso(), row["id"]))
        conn.commit()
    token = issue_session(row["id"])
    response = RedirectResponse(_safe_next(stored.get("next")), status_code=302)
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


# ------------------------------------------------------------------ auth config (LDAP / SSO)
LDAP_FIELD_MAP = {
    "url": ("ldap.url", ldap_auth.LDAP_URL_ENV),
    "base_dn": ("ldap.base_dn", ldap_auth.BASE_DN_ENV),
    "user_dn": ("ldap.user_dn", ldap_auth.USER_DN_ENV),
    "user_filter": ("ldap.user_filter", ldap_auth.USER_FILTER_ENV),
    "bind_dn": ("ldap.bind_dn", ldap_auth.SERVICE_DN_ENV),
    "bind_password": ("ldap.bind_password", ldap_auth.SERVICE_PW_ENV),
    "admin_group": ("ldap.admin_group", ldap_auth.ADMIN_GROUP_ENV),
    "starttls": ("ldap.starttls", ldap_auth.STARTTLS_ENV),
    "timeout": ("ldap.timeout", ldap_auth.TIMEOUT_ENV),
    "label": ("ldap.label", ldap_auth.LABEL_ENV),
}
SSO_FIELD_MAP = {
    "issuer": ("sso.issuer", sso_auth.ISSUER_ENV),
    "client_id": ("sso.client_id", sso_auth.CLIENT_ID_ENV),
    "client_secret": ("sso.client_secret", sso_auth.CLIENT_SECRET_ENV),
    "scopes": ("sso.scopes", sso_auth.SCOPES_ENV),
    "redirect_uri": ("sso.redirect_uri", sso_auth.REDIRECT_ENV),
    "admin_group": ("sso.admin_group", sso_auth.ADMIN_GROUP_ENV),
    "username_claim": ("sso.username_claim", sso_auth.USERNAME_CLAIM_ENV),
    "groups_claim": ("sso.groups_claim", sso_auth.GROUPS_CLAIM_ENV),
    "label": ("sso.label", sso_auth.LABEL_ENV),
    "token_auth": ("sso.token_auth", sso_auth.TOKEN_AUTH_ENV),
}


@admin_router.get("/auth-config")
def get_auth_config(admin: Dict[str, Any] = Depends(admin_guard)) -> Dict[str, Any]:
    """LDAP / SSO 两类设置：当前生效值 + 每个值的来源（页面 / 环境变量）。"""
    return {"ldap": ldap_auth.config_view(), "sso": sso_auth.config_view()}


def _save_section(payload: Dict[str, Any], field_map: Dict[str, tuple]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for field, (db_key, _env_name) in field_map.items():
        if field not in payload:
            continue
        value = payload[field]
        if field in {"bind_password", "client_secret"}:
            # 空串 = 不修改；前端传回遮罩值也视为"不修改"，避免把 •••• 存进去
            if not value or str(value).startswith("•"):
                continue
            values[db_key] = str(value)
            continue
        if isinstance(value, bool):
            values[db_key] = "1" if value else "0"
            continue
        values[db_key] = "" if value is None else str(value).strip()
    return values


@admin_router.put("/auth-config")
def put_auth_config(payload: Dict[str, Any], admin: Dict[str, Any] = Depends(admin_guard)) -> Dict[str, Any]:
    """保存 LDAP / SSO 设置到数据库（覆盖环境变量）。只传一个 section 也行。"""
    saved: Dict[str, str] = {}
    if isinstance(payload.get("ldap"), dict):
        saved.update(_save_section(payload["ldap"], LDAP_FIELD_MAP))
    if isinstance(payload.get("sso"), dict):
        saved.update(_save_section(payload["sso"], SSO_FIELD_MAP))
    if saved:
        settings_store.set_many(saved)
    sso_auth._DISCOVERY.clear()  # 改了 issuer 之后别再用旧缓存
    return {"saved": sorted(saved.keys()), "ldap": ldap_auth.config_view(), "sso": sso_auth.config_view()}


@admin_router.post("/auth-config/test")
def test_auth_config(payload: Dict[str, Any], admin: Dict[str, Any] = Depends(admin_guard)) -> Dict[str, Any]:
    kind = str(payload.get("kind") or "").lower()
    if kind == "ldap":
        return {"kind": "ldap", **ldap_auth.health()}
    if kind == "sso":
        return {"kind": "sso", **sso_auth.probe()}
    raise HTTPException(status_code=400, detail="kind 只能是 ldap 或 sso")


@admin_router.delete("/auth-config")
def clear_auth_config(kind: Optional[str] = None, admin: Dict[str, Any] = Depends(admin_guard)) -> Dict[str, Any]:
    """清掉页面里存的值，回退到环境变量（相当于"撤销页面配置"）。"""
    keys = []
    if kind in (None, "", "ldap"):
        keys += [v[0] for v in LDAP_FIELD_MAP.values()]
    if kind in (None, "", "sso"):
        keys += [v[0] for v in SSO_FIELD_MAP.values()]
    settings_store.delete(keys)
    sso_auth._DISCOVERY.clear()
    return {"cleared": len(keys), "ldap": ldap_auth.config_view(), "sso": sso_auth.config_view()}


# ------------------------------------------------------------------ pages
def login_page() -> FileResponse:
    return FileResponse(str(BASE_DIR / "frontend" / "login.html"), media_type="text/html")


def admin_page(user: Optional[sqlite3.Row]) -> Response:
    if user is None:
        return RedirectResponse("/login?next=/admin", status_code=302)
    if not user["enabled"] or user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return FileResponse(str(BASE_DIR / "frontend" / "admin.html"), media_type="text/html")
