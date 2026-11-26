"""LDAP 接入层的离线自检（不依赖真的 LDAP 服务器）。

用假的 Connection 顶替 ldap3，验证的是我们自己的控制流：
模板绑定 / 搜索绑定 / 密码错 / 组映射 / 影子账号落库 / 重复登录复用账号。
真机联调时把 LABELME_WEB_LDAP_* 指到真服务器，直接开页面登录即可。
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="lmweb-ldap-"))
os.environ["LABELME_WEB_DB"] = str(TMP / "test.db")
os.environ["LABELME_WEB_DATA"] = str(TMP / "data")
os.environ["LABELME_WEB_LDAP_URL"] = "ldap://fake.local:389"
os.environ["LABELME_WEB_LDAP_BASE_DN"] = "ou=people,dc=lab,dc=local"
os.environ["LABELME_WEB_LDAP_USER_DN"] = "uid={username},ou=people,dc=lab,dc=local"
os.environ["LABELME_WEB_LDAP_ADMIN_GROUP"] = "labelme-admins"

from backend import auth, ldap_auth  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {extra}"))


class Entry:
    def __init__(self, dn, attrs):
        self.entry_dn = dn
        self._attrs = attrs

    def __getattr__(self, item):
        return self._attrs.get(item)


class EntryList(list):
    pass


class FakeConn:
    """最小可用的 LDAP 连接：只实现 bind / unbind / search。"""

    DIRECTORY = {
        "uid=alice,ou=people,dc=lab,dc=local": {
            "userPassword": "secret123",
            "memberOf": ["cn=labelme-admins,ou=groups,dc=lab,dc=local"],
            "uid": "alice",
        },
        "uid=bob,ou=people,dc=lab,dc=local": {
            "userPassword": "bobpass123",
            "memberOf": [],
            "uid": "bob",
        },
    }
    OPEN_DN = "cn=svc,ou=services,dc=lab,dc=local"
    OPEN_PW = "svcpass123"

    def __init__(self, server, user=None, password=None, **_kw):
        self.user = user
        self.password = password
        self.entries = EntryList()
        self.bound = False

    def start_tls(self):
        return True

    def bind(self):
        if self.user is None:
            self.bound = True
            return True
        if self.user == self.OPEN_DN and self.password == self.OPEN_PW:
            self.bound = True
            return True
        entry = self.DIRECTORY.get(self.user)
        if entry and entry["userPassword"] == self.password:
            self.bound = True
            return True
        return False

    def unbind(self):
        self.bound = False

    def search(self, base, flt, attributes=None, size_limit=0):
        self.entries = EntryList()
        if base == self.OPEN_DN:
            self.entries.append(Entry(self.OPEN_DN, {}))
            return True
        if base in self.DIRECTORY:
            self.entries.append(Entry(base, self.DIRECTORY[base]))
            return True
        for dn, attrs in self.DIRECTORY.items():
            dn_l = dn.lower()
            base_l = base.lower() if isinstance(base, str) else ""
            if not (dn_l.endswith("," + base_l) or dn_l == base_l):
                continue
            if flt.startswith("(uid="):
                wanted = flt[len("(uid=") :]
                if wanted.endswith(")"):
                    wanted = wanted[:-1]
                if attrs.get("uid") == wanted:
                    self.entries.append(Entry(dn, attrs))
        return True


class FakeServer:
    pass


def fake_connection(server, user=None, password=None):
    """按真实 _connection 的契约来：绑定失败抛 LdapDenied，网络问题抛 LdapUnavailable。"""
    conn = FakeConn(server, user, password)
    if os.environ.get("LABELME_WEB_LDAP_STARTTLS", "").lower() in {"1", "true", "yes"}:
        conn.start_tls()
    if not conn.bind():
        raise ldap_auth.LdapDenied("invalidCredentials")
    return conn


ldap_auth._server = lambda: FakeServer()
ldap_auth._connection = fake_connection

# ---- 1. 模板绑定：密码对 / 密码错 / 账号不存在
u = ldap_auth.authenticate("alice", "secret123")
check("alice 认证通过", u.username == "alice" and u.dn.endswith("uid=alice,ou=people,dc=lab,dc=local"), str(u))
check("组映射到管理员", u.is_admin is True, str(u))

try:
    ldap_auth.authenticate("alice", "wrongpass")
    check("错误密码被拒", False, "没有抛异常")
except ldap_auth.LdapDenied:
    check("错误密码被拒", True)

try:
    ldap_auth.authenticate("nobody", "whatever")
    check("不存在的账号被拒", False, "没有抛异常")
except ldap_auth.LdapDenied:
    check("不存在的账号被拒", True)

bob = ldap_auth.authenticate("bob", "bobpass123")
check("bob 不是管理员", bob.is_admin is False, str(bob))

# ---- 2. 搜索绑定模式（AD 风格）：服务账号先查 DN，再绑
os.environ.pop("LABELME_WEB_LDAP_USER_DN", None)
os.environ["LABELME_WEB_LDAP_USER_FILTER"] = "(uid={username})"
os.environ["LABELME_WEB_LDAP_BIND_DN"] = FakeConn.OPEN_DN
os.environ["LABELME_WEB_LDAP_BIND_PASSWORD"] = FakeConn.OPEN_PW
u2 = ldap_auth.authenticate("bob", "bobpass123")
check("搜索绑定模式可用", u2.dn == "uid=bob,ou=people,dc=lab,dc=local", str(u2))

# 注入尝试：用户名里带 * 应该被转义，而不是匹配到所有人
escaped = ldap_auth.escape_filter("a*(b)")
check("过滤器转义", escaped == "a\\2a\\28b\\29", escaped)

# ---- 3. 影子账号落库 + 权限映射 + 重复登录复用
row = auth.ldap_shadow_user(u)
check("影子账号写入", row["source"] == "ldap" and row["external_id"].startswith("uid=alice"), dict(row))
check("影子账号是管理员", row["role"] == "admin", row["role"])
check("影子账号没有可用密码", auth.verify_password("secret123", row["password_hash"]) is False, row["password_hash"])

again = auth.ldap_shadow_user(u)
check("重复登录不重复建号", again["id"] == row["id"], f"{row['id']} vs {again['id']}")

row2 = auth.ldap_shadow_user(bob)
check("普通 LDAP 账号是标注员", row2["role"] == "annotator", row2["role"])

# ---- 4. /api/auth/methods 只暴露开关，不暴露地址
methods = auth.auth_methods()
check("methods 不泄露服务器地址", "ldap://" not in str(methods) and methods["ldap"]["enabled"] is True, str(methods))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
