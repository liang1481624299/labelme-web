"""LDAP / AD 接入层（可选，全部由环境变量开关）。

设计原则：
1. 默认关闭。只要没有 ``LABELME_WEB_LDAP_URL``，这个模块完全不参与登录流程，
   现有本地账号 + bcrypt 的路径一行都不变。
2. 只做"认证"，不做"账号目录"。LDAP 里验证通过后，本地 users 表建/更新一条影子账号
   （ ``source='ldap'``，``password_hash`` 存一个永不可能匹配的占位符），
   之后权限、工程、会话、/admin 全部复用原有逻辑。
3. LDAP 挂掉不能把登录页拖死：连接失败抛出 ``LdapUnavailable``，上层降级成
   "LDAP 暂时不可用，请用本地账号登录"，而不是 500。

支持的两种绑定方式：
- 模板绑定（默认）：``LABELME_WEB_LDAP_USER_DN`` = ``uid={username},ou=people,dc=lab,dc=local``
- 搜索绑定（AD 常用）：给了 ``LABELME_WEB_LDAP_USER_FILTER`` 就先以服务账号（或匿名）
  搜出真实 DN，再用用户密码绑定。AD 的过滤器形如 ``(sAMAccountName={username})``。

环境变量一览见 README 的「LDAP 接入」一节。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend import settings_store

LDAP_URL_ENV = "LABELME_WEB_LDAP_URL"
BASE_DN_ENV = "LABELME_WEB_LDAP_BASE_DN"
USER_DN_ENV = "LABELME_WEB_LDAP_USER_DN"
USER_FILTER_ENV = "LABELME_WEB_LDAP_USER_FILTER"
SERVICE_DN_ENV = "LABELME_WEB_LDAP_BIND_DN"
SERVICE_PW_ENV = "LABELME_WEB_LDAP_BIND_PASSWORD"
ADMIN_GROUP_ENV = "LABELME_WEB_LDAP_ADMIN_GROUP"
STARTTLS_ENV = "LABELME_WEB_LDAP_STARTTLS"
TIMEOUT_ENV = "LABELME_WEB_LDAP_TIMEOUT"
LABEL_ENV = "LABELME_WEB_LDAP_LABEL"

DEFAULT_TIMEOUT = 5.0
SHADOW_HASH = "!ldap"  # 占位：不是合法 bcrypt，verify_password 永远返回 False


class LdapUnavailable(Exception):
    """连不上 / 超时 / TLS 失败 —— 上层要降级，不要当成密码错。"""


class LdapDenied(Exception):
    """账号不存在或密码错。"""


@dataclass
class LdapUser:
    username: str
    dn: str
    is_admin: bool


def _env(key: str, default: str = "") -> str:
    """环境变量 → 数据库键的映射；数据库里改过的值优先。"""
    db_key = {
        LDAP_URL_ENV: "ldap.url",
        BASE_DN_ENV: "ldap.base_dn",
        USER_DN_ENV: "ldap.user_dn",
        USER_FILTER_ENV: "ldap.user_filter",
        SERVICE_DN_ENV: "ldap.bind_dn",
        SERVICE_PW_ENV: "ldap.bind_password",
        ADMIN_GROUP_ENV: "ldap.admin_group",
        STARTTLS_ENV: "ldap.starttls",
        TIMEOUT_ENV: "ldap.timeout",
        LABEL_ENV: "ldap.label",
    }.get(key, key)
    return settings_store.get(db_key, key, default)


def enabled() -> bool:
    return bool(_env(LDAP_URL_ENV))


def label() -> str:
    """登录页上显示的名字，比如「学院 LDAP」。"""
    return _env(LABEL_ENV, "LDAP / 域账号")


def public_config() -> dict:
    """给 /api/auth/methods 用：只暴露"开没开"和显示名，不泄露服务器地址。"""
    if not enabled():
        return {"enabled": False, "label": ""}
    return {"enabled": True, "label": label()}


def config_view() -> Dict[str, Any]:
    """管理端展示用：只把密码做遮罩，其余原样给出，并标出每个值的来源。"""
    return {
        "enabled": enabled(),
        "url": _env(LDAP_URL_ENV),
        "base_dn": _env(BASE_DN_ENV),
        "user_dn": _env(USER_DN_ENV),
        "user_filter": _env(USER_FILTER_ENV),
        "bind_dn": _env(SERVICE_DN_ENV),
        "bind_password": settings_store.mask(_env(SERVICE_PW_ENV)),
        "bind_password_set": bool(_env(SERVICE_PW_ENV)),
        "admin_group": _env(ADMIN_GROUP_ENV),
        "starttls": _env(STARTTLS_ENV).lower() in {"1", "true", "yes"},
        "timeout": _env(TIMEOUT_ENV, str(DEFAULT_TIMEOUT)),
        "label": label(),
        "source": {
            "url": settings_store.source_of("ldap.url", LDAP_URL_ENV),
            "base_dn": settings_store.source_of("ldap.base_dn", BASE_DN_ENV),
            "user_dn": settings_store.source_of("ldap.user_dn", USER_DN_ENV),
        },
    }


def escape_filter(value: str) -> str:
    """RFC 4515 转义，防过滤器注入。"""
    return (
        value.replace("\\", "\\5c")
        .replace("*", "\\2a")
        .replace("(", "\\28")
        .replace(")", "\\29")
        .replace("\x00", "\\00")
    )


def _timeout() -> float:
    try:
        return float(_env(TIMEOUT_ENV, str(DEFAULT_TIMEOUT)))
    except ValueError:
        return DEFAULT_TIMEOUT


def _server():
    # 延迟导入：没开 LDAP 的部署甚至不需要装 ldap3
    from ldap3 import Server

    url = _env(LDAP_URL_ENV)
    if not url:
        raise LdapUnavailable("未配置 LDAP 地址")
    return Server(url, connect_timeout=_timeout(), get_info=None)


def _connection(server, user: Optional[str] = None, password: Optional[str] = None):
    from ldap3 import Connection

    conn = Connection(
        server,
        user=user,
        password=password,
        # ldap3 的 receive_timeout 会直接进 socket；Python 3.14 下传 float
        # 会报 "required argument is not an integer"，这里统一取整。
        receive_timeout=max(1, int(_timeout())),
        auto_bind=False,
        raise_exceptions=False,
        read_only=True,
    )
    try:
        if _env(STARTTLS_ENV).lower() in {"1", "true", "yes"} and not conn.start_tls():
            raise LdapUnavailable("StartTLS 握手失败")
        if not conn.bind():
            raise LdapDenied(_result_message(conn))
    except LdapDenied:
        raise
    except LdapUnavailable:
        raise
    except Exception as exc:  # 网络层错误统一按"不可用"处理
        raise LdapUnavailable(str(exc))
    return conn


def _result_message(conn) -> str:
    try:
        desc = conn.result.get("description") or ""
        msg = conn.result.get("message") or ""
        return (desc + (" / " + msg if msg else "")).strip() or "绑定失败"
    except Exception:
        return "绑定失败"


def _resolve_dn(conn, username: str) -> str:
    """模板模式直接拼 DN；搜索模式先查再绑。"""
    template = _env(USER_DN_ENV)
    base = _env(BASE_DN_ENV)
    if template:
        return template.replace("{username}", username).replace("{base_dn}", base)
    flt = _env(USER_FILTER_ENV)
    if not flt or not base:
        raise LdapUnavailable("LDAP 需要配置 LABELME_WEB_LDAP_USER_DN 或（USER_FILTER + BASE_DN）")
    flt = flt.replace("{username}", escape_filter(username))
    ok = conn.search(base, flt, attributes=["distinguishedName"], size_limit=5)
    if not ok or not conn.entries:
        raise LdapDenied("LDAP 里没有这个账号")
    if len(conn.entries) > 1:
        raise LdapDenied("LDAP 过滤器命中了多个账号，请收紧 USER_FILTER")
    return str(conn.entries[0].entry_dn)


def _check_admin(conn, user_dn: str, group: str) -> bool:
    """group 给 cn 就看 memberOf，给 DN 就在组里查 member。"""
    if not group:
        return False
    if "=" in group:
        try:
            if conn.search(group, "(member=" + escape_filter(user_dn) + ")", attributes=["cn"], size_limit=1):
                return bool(conn.entries)
        except Exception:
            pass
        return False
    try:
        if conn.search(user_dn, "(objectClass=*)", attributes=["memberOf"], size_limit=1) and conn.entries:
            raw = conn.entries[0].memberOf
            values = getattr(raw, "values", None)
            if values is None:
                values = list(raw) if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
            needle = group.lower()
            return any(needle in str(g or "").lower() for g in values)
    except Exception:
        pass
    return False


def authenticate(username: str, password: str) -> LdapUser:
    """对 LDAP 做一次真实绑定。成功返回上下文，失败抛 LdapDenied / LdapUnavailable。"""
    if not enabled():
        raise LdapUnavailable("未启用 LDAP")
    if not username or not password:
        raise LdapDenied("账号或密码为空")

    server = _server()
    template = _env(USER_DN_ENV)
    flt = _env(USER_FILTER_ENV)
    service_dn = _env(SERVICE_DN_ENV)
    service_pw = _env(SERVICE_PW_ENV)

    if template:
        # 模板模式：不需要任何搜索，也不需要匿名绑定（AD 默认拒绝匿名 simple bind）
        user_dn = template.replace("{username}", username).replace("{base_dn}", _env(BASE_DN_ENV))
    elif flt:
        # 搜索模式：先用服务账号（或匿名）把真实 DN 查出来
        search_conn = _connection(server, service_dn or None, service_pw or None)
        try:
            user_dn = _resolve_dn(search_conn, username)
        finally:
            try:
                search_conn.unbind()
            except Exception:
                pass
    else:
        raise LdapUnavailable("LDAP 需要配置 LABELME_WEB_LDAP_USER_DN 或（USER_FILTER + BASE_DN）")

    # 用用户自己的密码绑定 —— 这一步就是认证本身
    user_conn = _connection(server, user_dn, password)
    try:
        is_admin = _check_admin(user_conn, user_dn, _env(ADMIN_GROUP_ENV))
    finally:
        try:
            user_conn.unbind()
        except Exception:
            pass
    return LdapUser(username=username, dn=user_dn, is_admin=is_admin)


def health() -> dict:
    """给管理端/运维用的连通性探测：只 bind 服务账号（或匿名），不验证具体用户。"""
    if not enabled():
        return {"enabled": False, "ok": None, "detail": "未启用"}
    try:
        conn = _connection(_server(), _env(SERVICE_DN_ENV) or None, _env(SERVICE_PW_ENV) or None)
        try:
            base = _env(BASE_DN_ENV)
            reachable = True
            if base:
                reachable = bool(conn.search(base, "(objectClass=*)", attributes=["cn"], size_limit=1))
            return {"enabled": True, "ok": reachable, "detail": "连接正常" if reachable else "已连接但 base DN 搜不到"}
        finally:
            try:
                conn.unbind()
            except Exception:
                pass
    except LdapUnavailable as exc:
        return {"enabled": True, "ok": False, "detail": f"连接失败：{exc}"}
    except LdapDenied as exc:
        return {"enabled": True, "ok": False, "detail": f"服务账号绑定失败：{exc}"}
