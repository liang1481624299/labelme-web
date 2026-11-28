"""OIDC（OpenID Connect）SSO 接入层 —— 通用实现，不绑定某一家 IdP。

对接过 Authentik / Keycloak / Google / Microsoft Entra 都走同一套：discovery 文档 →
authorization code 流程 → 用 IdP 的 JWKS 校验 id_token → 取 claims 建影子账号。

配置同样走 settings_store（页面可改、环境变量兜底），密钥只遮罩展示。
没配 ``sso.issuer`` + ``sso.client_id`` 就不启用，登录页也不会出现 SSO 按钮。
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from backend import settings_store

ISSUER_KEY, ISSUER_ENV = "sso.issuer", "LABELME_WEB_SSO_ISSUER"
CLIENT_ID_KEY, CLIENT_ID_ENV = "sso.client_id", "LABELME_WEB_SSO_CLIENT_ID"
CLIENT_SECRET_KEY, CLIENT_SECRET_ENV = "sso.client_secret", "LABELME_WEB_SSO_CLIENT_SECRET"
SCOPES_KEY, SCOPES_ENV = "sso.scopes", "LABELME_WEB_SSO_SCOPES"
REDIRECT_KEY, REDIRECT_ENV = "sso.redirect_uri", "LABELME_WEB_SSO_REDIRECT_URI"
ADMIN_GROUP_KEY, ADMIN_GROUP_ENV = "sso.admin_group", "LABELME_WEB_SSO_ADMIN_GROUP"
USERNAME_CLAIM_KEY, USERNAME_CLAIM_ENV = "sso.username_claim", "LABELME_WEB_SSO_USERNAME_CLAIM"
GROUPS_CLAIM_KEY, GROUPS_CLAIM_ENV = "sso.groups_claim", "LABELME_WEB_SSO_GROUPS_CLAIM"
LABEL_KEY, LABEL_ENV = "sso.label", "LABELME_WEB_SSO_LABEL"
TOKEN_AUTH_KEY, TOKEN_AUTH_ENV = "sso.token_auth", "LABELME_WEB_SSO_TOKEN_AUTH"

DEFAULT_SCOPES = "openid profile email"
DISCOVERY_TTL = 600          # discovery 文档缓存 10 分钟
STATE_TTL = 600              # 授权 state 有效期 10 分钟


class SsoUnavailable(Exception):
    """IdP 连不上 / discovery 失败 / 回调参数不对。"""


class SsoError(Exception):
    """拿到响应了，但 token 或 id_token 校验不过。"""


@dataclass
class SsoUser:
    username: str
    sub: str
    is_admin: bool
    claims: Dict[str, Any]


def _cfg(key: str, env_name: str, default: str = "") -> str:
    return settings_store.get(key, env_name, default)


def enabled() -> bool:
    return bool(_cfg(ISSUER_KEY, ISSUER_ENV) and _cfg(CLIENT_ID_KEY, CLIENT_ID_ENV))


def label() -> str:
    return _cfg(LABEL_KEY, LABEL_ENV, "统一登录（SSO）")


def public_config() -> dict:
    if not enabled():
        return {"enabled": False, "label": ""}
    return {"enabled": True, "label": label()}


def redirect_uri(request_origin: str) -> str:
    """显式配了就用配的，否则按当前请求的 origin 拼 —— 反代后记得让 X-Forwarded-* 透传。"""
    configured = _cfg(REDIRECT_KEY, REDIRECT_ENV)
    if configured:
        return configured
    return request_origin.rstrip("/") + "/api/auth/sso/callback"


# ------------------------------------------------------------------ discovery
_DISCOVERY: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _http_json(url: str, timeout: float = 8.0, data: Optional[bytes] = None,
               headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SsoError(f"IdP 返回 {exc.code}：{detail}")
    except urllib.error.URLError as exc:
        raise SsoUnavailable(f"连不上 IdP：{exc.reason}")
    except OSError as exc:
        raise SsoUnavailable(f"连不上 IdP：{exc}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise SsoError("IdP 返回的不是 JSON")


def discovery(force: bool = False) -> Dict[str, Any]:
    issuer = _cfg(ISSUER_KEY, ISSUER_ENV).rstrip("/")
    if not issuer:
        raise SsoUnavailable("未配置 SSO issuer")
    hit = _DISCOVERY.get(issuer)
    if hit and not force and time.time() - hit[0] < DISCOVERY_TTL:
        return hit[1]
    url = issuer + "/.well-known/openid-configuration"
    doc = _http_json(url)
    for field in ("authorization_endpoint", "token_endpoint"):
        if not doc.get(field):
            raise SsoError(f"discovery 文档缺少 {field}")
    if doc.get("issuer") and str(doc["issuer"]).rstrip("/") != issuer:
        raise SsoError("discovery 里的 issuer 与你配置的不一致")
    _DISCOVERY[issuer] = (time.time(), doc)
    return doc


# ------------------------------------------------------------------ state
_STATES: Dict[str, Dict[str, Any]] = {}


def _prune_states() -> None:
    now = time.time()
    for key in [k for k, v in _STATES.items() if now - v["at"] > STATE_TTL]:
        _STATES.pop(key, None)
    if len(_STATES) >= 4096:
        # 登录入口是公开的；限制未完成 state 的数量，避免被刷爆内存。
        for key in sorted(_STATES, key=lambda k: _STATES[k]["at"])[: len(_STATES) - 2048]:
            _STATES.pop(key, None)


def begin(origin: str, next_path: str, bind_user_id: Optional[int] = None) -> str:
    """生成 state 并返回要跳转的 authorization URL。

    bind_user_id 有值时表示“把 SSO 身份绑定到已登录账号”，回调不新建/切换会话。
    """
    doc = discovery()
    _prune_states()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(16)
    redirect = redirect_uri(origin)
    _STATES[state] = {
        "nonce": nonce,
        "next": next_path or "/",
        "at": time.time(),
        "redirect": redirect,
        "bind_user_id": bind_user_id,
    }
    params = {
        "response_type": "code",
        "client_id": _cfg(CLIENT_ID_KEY, CLIENT_ID_ENV),
        "redirect_uri": redirect,
        "scope": _cfg(SCOPES_KEY, SCOPES_ENV, DEFAULT_SCOPES),
        "state": state,
        "nonce": nonce,
    }
    return doc["authorization_endpoint"] + ("&" if "?" in doc["authorization_endpoint"] else "?") + urllib.parse.urlencode(params)


def take_state(state: str) -> Dict[str, Any]:
    _prune_states()
    item = _STATES.pop(state or "", None)
    if item is None:
        raise SsoUnavailable("登录状态已过期，请重新点一次 SSO 登录")
    return item


# ------------------------------------------------------------------ token / claims
def _exchange_code(code: str, stored: Dict[str, Any]) -> Dict[str, Any]:
    doc = discovery()
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": stored["redirect"],
        "client_id": _cfg(CLIENT_ID_KEY, CLIENT_ID_ENV),
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    secret = _cfg(CLIENT_SECRET_KEY, CLIENT_SECRET_ENV)
    mode = (_cfg(TOKEN_AUTH_KEY, TOKEN_AUTH_ENV, "post") or "post").lower()
    if secret and mode == "basic":
        basic = f"{urllib.parse.quote(payload['client_id'])}:{urllib.parse.quote(secret)}"
        headers["Authorization"] = "Basic " + __import__("base64").b64encode(basic.encode()).decode()
    elif secret:
        payload["client_secret"] = secret
    return _http_json(doc["token_endpoint"], data=urllib.parse.urlencode(payload).encode(), headers=headers)


def _verify_id_token(id_token: str, nonce: str) -> Dict[str, Any]:
    try:
        import jwt  # PyJWT
        from jwt import PyJWKClient
    except ImportError as exc:  # 运行环境没装依赖
        raise SsoUnavailable(f"服务端缺少 PyJWT（pip install PyJWT cryptography）：{exc}")

    doc = discovery()
    jwks_uri = doc.get("jwks_uri")
    if not jwks_uri:
        raise SsoError("discovery 文档缺少 jwks_uri，无法校验 id_token")
    try:
        client = PyJWKClient(jwks_uri)
        key = client.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"],
            audience=_cfg(CLIENT_ID_KEY, CLIENT_ID_ENV),
            issuer=doc.get("issuer"),
            options={"require": ["exp", "iat"]},
        )
    except SsoError:
        raise
    except Exception as exc:
        raise SsoError(f"id_token 校验失败：{exc}")
    if nonce and claims.get("nonce") != nonce:
        raise SsoError("nonce 不匹配（疑似重放）")
    return claims


def _username_from(claims: Dict[str, Any]) -> str:
    claim = _cfg(USERNAME_CLAIM_KEY, USERNAME_CLAIM_ENV, "")
    candidates = []
    if claim:
        candidates.append(claim)
    candidates += ["preferred_username", "username", "email", "name", "sub"]
    for key in candidates:
        value = claims.get(key)
        if value:
            return str(value)
    raise SsoError("id_token 里没有可用的账号名字段")


def _is_admin(claims: Dict[str, Any], username: str) -> bool:
    group = _cfg(ADMIN_GROUP_KEY, ADMIN_GROUP_ENV)
    if not group:
        return False
    claim = _cfg(GROUPS_CLAIM_KEY, GROUPS_CLAIM_ENV, "groups")
    groups = claims.get(claim)
    if groups is None:
        # 有些 IdP 把组塞在 realm_access.roles / roles 里
        for alt in ("roles", "realm_access", "resource_access"):
            value = claims.get(alt)
            if isinstance(value, dict):
                groups = value.get("roles")
            elif value is not None:
                groups = value
            if groups:
                break
    if isinstance(groups, str):
        groups = [groups]
    if not isinstance(groups, (list, tuple)):
        return False
    needle = group.lower()
    return any(needle == str(g).lower() or needle in str(g).lower() for g in groups)


def finish(code: str, state: str) -> Tuple[SsoUser, Dict[str, Any]]:
    """校验回调并返回 (用户, 本次登录的上下文)，上下文里有发起时记下的 next 路径。"""
    stored = take_state(state)
    if not code:
        raise SsoUnavailable("IdP 没有返回 code（可能被拒绝授权）")
    tokens = _exchange_code(code, stored)
    id_token = tokens.get("id_token")
    if not id_token:
        raise SsoError("token 响应里没有 id_token（scope 里要有 openid）")
    claims = _verify_id_token(id_token, stored.get("nonce") or "")
    username = _username_from(claims)
    user = SsoUser(username=username, sub=str(claims.get("sub") or username), is_admin=_is_admin(claims, username), claims=claims)
    return user, stored


def config_view() -> Dict[str, Any]:
    return {
        "enabled": enabled(),
        "issuer": _cfg(ISSUER_KEY, ISSUER_ENV),
        "client_id": _cfg(CLIENT_ID_KEY, CLIENT_ID_ENV),
        "client_secret": settings_store.mask(_cfg(CLIENT_SECRET_KEY, CLIENT_SECRET_ENV)),
        "client_secret_set": bool(_cfg(CLIENT_SECRET_KEY, CLIENT_SECRET_ENV)),
        "scopes": _cfg(SCOPES_KEY, SCOPES_ENV, DEFAULT_SCOPES),
        "redirect_uri": _cfg(REDIRECT_KEY, REDIRECT_ENV),
        "admin_group": _cfg(ADMIN_GROUP_KEY, ADMIN_GROUP_ENV),
        "username_claim": _cfg(USERNAME_CLAIM_KEY, USERNAME_CLAIM_ENV, "preferred_username"),
        "groups_claim": _cfg(GROUPS_CLAIM_KEY, GROUPS_CLAIM_ENV, "groups"),
        "label": label(),
        "token_auth": (_cfg(TOKEN_AUTH_KEY, TOKEN_AUTH_ENV, "post") or "post").lower(),
        "source": {
            "issuer": settings_store.source_of(ISSUER_KEY, ISSUER_ENV),
            "client_id": settings_store.source_of(CLIENT_ID_KEY, CLIENT_ID_ENV),
        },
    }


def probe() -> Dict[str, Any]:
    """管理端「测试连接」：拉一次 discovery，顺带把关键端点回显。"""
    if not enabled():
        return {"ok": None, "detail": "未启用"}
    try:
        doc = discovery(force=True)
        return {
            "ok": True,
            "detail": "discovery 正常",
            "issuer": doc.get("issuer"),
            "authorization_endpoint": doc.get("authorization_endpoint"),
            "token_endpoint": doc.get("token_endpoint"),
            "jwks_uri": doc.get("jwks_uri"),
            "scopes_supported": (doc.get("scopes_supported") or [])[:12],
        }
    except (SsoUnavailable, SsoError) as exc:
        return {"ok": False, "detail": str(exc)}
