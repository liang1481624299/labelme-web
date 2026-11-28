"""账号绑定离线自检。

覆盖：本地账号主动绑定 LDAP、同名外部身份不能自动接管、
绑定冲突、解绑后恢复未绑定、影子账号自动建号。
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="lmweb-bind-"))
os.environ["LABELME_WEB_DB"] = str(TMP / "test.db")
os.environ["LABELME_WEB_DATA"] = str(TMP / "data")
os.environ["LABELME_WEB_LDAP_URL"] = "ldap://fake.local:389"
os.environ["LABELME_WEB_LDAP_USER_DN"] = "uid={username},ou=people,dc=lab,dc=local"
os.environ["LABELME_WEB_SSO_ISSUER"] = "https://idp.example.test"
os.environ["LABELME_WEB_SSO_CLIENT_ID"] = "labelme-test"

from fastapi.testclient import TestClient

from backend import auth, ldap_auth, sso_auth
from backend.app import app

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {extra}"))


def fake_authenticate(username, password):
    if password != "ldap-pass":
        raise ldap_auth.LdapDenied("invalid")
    return ldap_auth.LdapUser(
        username=username,
        dn=f"uid={username},ou=people,dc=lab,dc=local",
        is_admin=False,
    )


ldap_auth.authenticate = fake_authenticate

client = TestClient(app)
r = client.post("/api/auth/register", json={"username": "alice", "password": "local-pass-123"})
check("本地账号注册", r.status_code == 201, r.text)
csrf = client.get("/api/auth/me").json()["csrf_token"]
local_id = auth.find_user("alice")["id"]

# 同名 LDAP 身份在未绑定前不能自动接管本地账号
try:
    auth.ldap_shadow_user(ldap_auth.authenticate("alice", "ldap-pass"))
    check("同名外部身份不能自动接管", False, "居然返回了本地账号")
except Exception as exc:
    check("同名外部身份不能自动接管", getattr(exc, "status_code", None) == 403, repr(exc))

r = client.post(
    "/api/auth/bind/ldap",
    json={"username": "alice", "password": "ldap-pass"},
    headers={"X-CSRF-Token": csrf},
)
check("登录后主动绑定 LDAP", r.status_code == 200 and r.json()["bindings"]["ldap"]["bound"], r.text)
check(
    "绑定后 LDAP 登录落到同一账号",
    auth.ldap_shadow_user(ldap_auth.authenticate("alice", "ldap-pass"))["id"] == local_id,
)

payload = auth.bindings_payload(local_id)
check("绑定状态可读", payload["bindings"]["ldap"]["external_name"] == "alice", str(payload))

# 解绑后恢复未绑定
r = client.delete("/api/auth/bindings/ldap", headers={"X-CSRF-Token": csrf})
check("本地账号可以解绑 LDAP", r.status_code == 200, r.text)
check("解绑后状态未绑定", r.json()["bindings"]["ldap"]["bound"] is False, r.text)

# 一个外部身份不能同时绑两个账号
auth.link_identity(local_id, "ldap", "uid=alice,ou=people,dc=lab,dc=local", "alice")
client.post("/api/auth/register", json={"username": "bob", "password": "local-pass-456"})
bob_id = auth.find_user("bob")["id"]
try:
    auth.link_identity(bob_id, "ldap", "uid=alice,ou=people,dc=lab,dc=local", "alice")
    check("外部身份不能被两个账号同时绑定", False, "居然绑定成功")
except Exception as exc:
    check("外部身份不能被两个账号同时绑定", getattr(exc, "status_code", None) == 409, repr(exc))

# 全新外部账号仍可自动建影子账号并绑定
carol = ldap_auth.authenticate("carol", "ldap-pass")
carol_row = auth.ldap_shadow_user(carol)
check("全新外部账号自动建影子账号", carol_row["source"] == "ldap" and carol_row["username"] == "carol", dict(carol_row))
check("全新外部账号有绑定记录", auth.bindings_payload(carol_row["id"])["bindings"]["ldap"]["bound"], "没有绑定")

# SSO 绑定：回调必须确认当前会话与发起绑定的人一致，且不能再发一个新会话
sso_auth.finish = lambda code, state: (
    sso_auth.SsoUser(
        username="bob-sso",
        sub="sso-sub-bob-001",
        is_admin=False,
        claims={},
    ),
    {"bind_user_id": bob_id, "next": "/?settings=account&bind=sso&ok=1"},
)
r = client.get("/api/auth/sso/callback?code=fake&state=fake", follow_redirects=False)
check("SSO 绑定回调成功", r.status_code == 302 and "bind=sso&ok=1" in r.headers.get("location", ""), r.text)
check("SSO 绑定后落到同一账号", auth.sso_shadow_user(sso_auth.SsoUser(
    username="bob-sso", sub="sso-sub-bob-001", is_admin=False, claims={}
))["id"] == bob_id)
check("SSO 绑定状态可读", auth.bindings_payload(bob_id)["bindings"]["sso"]["bound"], "没有绑定")

# 换一个已登录会话回调，必须拒绝把 SSO 身份绑到发起人以外的账号
client2 = TestClient(app)
r = client2.post("/api/auth/register", json={"username": "dave", "password": "Test12345!"})
check("准备第二会话", r.status_code == 201 or r.status_code == 200, r.text)
sso_auth.finish = lambda code, state: (
    sso_auth.SsoUser(
        username="dave-other-sso",
        sub="sso-sub-other-001",
        is_admin=False,
        claims={},
    ),
    {"bind_user_id": bob_id, "next": "/?settings=account&bind=sso&ok=1"},
)
r = client2.get("/api/auth/sso/callback?code=fake&state=fake", follow_redirects=False)
check("SSO 绑定拒绝他人会话", r.status_code == 302 and "error=" in r.headers.get("location", ""), r.text)
check("他人会话未写入绑定", auth.bindings_payload(bob_id)["bindings"]["sso"]["external_name"] == "bob-sso",
      auth.bindings_payload(bob_id))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
