"""SSO（OIDC）接入层离线自检：本地起一个假 IdP，跑完整 discovery → code 交换 → id_token 校验链路。

不依赖网络、不依赖真实 IdP。用 venv 跑：

    .venv\\Scripts\\python.exe dev\\sso_mock_check.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="lmweb-sso-test-"))
os.environ["LABELME_WEB_DB"] = str(TMP / "var" / "test.db")
os.environ["LABELME_WEB_DATA"] = str(TMP / "data")
(TMP / "var").mkdir(parents=True, exist_ok=True)
(TMP / "data").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
import jwt  # noqa: E402
from jwt import PyJWKClient  # noqa: E402

from backend import auth, settings_store, sso_auth  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "mock-key-1"
CLIENT_ID = "labelme-web"
ISSUER = "http://127.0.0.1:0"  # 占位，下面用真实端口覆盖
LAST = {"form": {}, "auth": ""}
ISSUED = {"nonce": "", "admin": False}


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwk() -> dict:
    pub = KEY.public_key().public_numbers()
    return {
        "kty": "RSA", "kid": KID, "use": "sig", "alg": "RS256",
        "n": b64u(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")),
        "e": b64u(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big")),
    }


def make_token(claims: dict, key=None) -> str:
    return jwt.encode(claims, key or KEY, algorithm="RS256", headers={"kid": KID})


class Handler(BaseHTTPRequestHandler):
    issuer = ISSUER

    def log_message(self, *args):  # 静音
        pass

    def _json(self, payload: dict, code: int = 200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/.well-known/openid-configuration":
            self._json({
                "issuer": self.issuer,
                "authorization_endpoint": self.issuer + "/authorize",
                "token_endpoint": self.issuer + "/token",
                "jwks_uri": self.issuer + "/jwks",
                "scopes_supported": ["openid", "profile", "email", "groups"],
            })
        elif path == "/jwks":
            self._json({"keys": [jwk()]})
        else:
            self._json({"error": "not_found"}, 404)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/token":
            self._json({"error": "not_found"}, 404)
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        LAST["form"] = form
        LAST["auth"] = self.headers.get("Authorization") or ""
        now = datetime.now(timezone.utc)
        claims = {
            "iss": self.issuer,
            "aud": CLIENT_ID,
            "sub": "u-1001",
            "preferred_username": form.get("code", "").replace("code-", ""),
            "email": "someone@lab.local",
            "nonce": ISSUED["nonce"],
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "groups": ["staff", "labelme-admins"] if ISSUED["admin"] else ["staff"],
        }
        self._json({"access_token": "at-" + form.get("code", ""), "id_token": make_token(claims), "token_type": "Bearer"})


SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = SERVER.server_address[1]
ISSUER = f"http://127.0.0.1:{PORT}"
Handler.issuer = ISSUER
SERVER.issuer = ISSUER
threading.Thread(target=SERVER.serve_forever, daemon=True).start()


def ok(cond, name: str, extra: str = "") -> bool:
    print(("PASS " if cond else "FAIL ") + name + (("  <- " + extra) if (extra and not cond) else ""))
    return bool(cond)


def expect_raises(fn, exc_type, needle: str = "") -> bool:
    try:
        fn()
    except exc_type as exc:
        return needle.lower() in str(exc).lower()
    except Exception as exc:  # 类型不对也算失败
        print(f"     抛了 {type(exc).__name__}: {exc}")
    return False


result = []
result.append(ok(not sso_auth.enabled(), "未配置时 enabled() == False"))
result.append(ok(sso_auth.public_config().get("enabled") is False, "未配置时 public_config 不暴露端点"))

settings_store.set_many({
    "sso.issuer": ISSUER,
    "sso.client_id": CLIENT_ID,
    "sso.client_secret": "s3cr3t-value",
    "sso.admin_group": "labelme-admins",
    "sso.label": "实训室统一登录",
})
sso_auth._DISCOVERY.clear()

doc = sso_auth.discovery()
result.append(ok(doc["token_endpoint"].endswith("/token"), "discovery 拿到 token_endpoint"))
stamp = sso_auth._DISCOVERY[ISSUER][0]
doc2 = sso_auth.discovery()
result.append(ok(sso_auth._DISCOVERY[ISSUER][0] == stamp, "discovery 命中缓存（没有重复请求）"))
result.append(ok("labelme-admins" in json.dumps(sso_auth.config_view()), "config_view 带出 admin_group"))
result.append(ok(sso_auth.config_view()["client_secret_set"] is True
                and "s3cr3t" not in sso_auth.config_view()["client_secret"], "config_view 遮罩 client_secret"))

ISSUED["nonce"] = ""
url = sso_auth.begin(ISSUER, "/?project=默认工程")
query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
state = query.get("state", "")
ISSUED["nonce"] = sso_auth._STATES[state]["nonce"]
ISSUED["admin"] = True
result.append(ok(query.get("response_type") == "code" and query.get("client_id") == CLIENT_ID, "authorization URL 参数正确"))
result.append(ok(query.get("redirect_uri", "").endswith("/api/auth/sso/callback"), "redirect_uri 落到 /api/auth/sso/callback"))
result.append(ok(bool(query.get("nonce")) and query.get("nonce") == ISSUED["nonce"], "nonce 已下发且与 state 里记录一致"))

user, stored = sso_auth.finish("code-alice", state)
result.append(ok(user.username == "alice" and stored["next"].startswith("/"), "code 交换 + id_token 校验通过，带出 next"))
result.append(ok(LAST["form"].get("client_secret") == "s3cr3t-value", "默认用 post 方式带 client_secret"))
result.append(ok(user.is_admin is True, "组命中 admin_group → is_admin"))
ISSUED["admin"] = False
result.append(ok(expect_raises(lambda: sso_auth.finish("code-alice", state), sso_auth.SsoUnavailable, "过期"), "state 只能用一次"))

sso_auth.begin(ISSUER, "/")
state2 = next(reversed(sso_auth._STATES))
ISSUED["nonce"] = sso_auth._STATES[state2]["nonce"]
settings_store.set_many({"sso.admin_group": "someone-else"})
user2, _ = sso_auth.finish("code-bob", state2)
result.append(ok(user2.is_admin is False, "组不命中 → 非管理员"))
settings_store.set_many({"sso.admin_group": "labelme-admins"})

settings_store.set_many({"sso.token_auth": "basic"})
sso_auth.begin(ISSUER, "/")
state3 = next(reversed(sso_auth._STATES))
ISSUED["nonce"] = sso_auth._STATES[state3]["nonce"]
sso_auth.finish("code-carol", state3)
result.append(ok(LAST["auth"].startswith("Basic "), "token_auth=basic 时走 Authorization 头"))
settings_store.set_many({"sso.token_auth": "post"})

now = datetime.now(timezone.utc)
base = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "preferred_username": "u", "iat": now, "exp": now + timedelta(minutes=5)}
result.append(ok(expect_raises(lambda: sso_auth._verify_id_token(make_token({**base, "aud": "other-app"}), ""), sso_auth.SsoError, "audience"),
              "audience 不匹配被拒"))
result.append(ok(expect_raises(lambda: sso_auth._verify_id_token(make_token({**base, "iss": "http://evil"}), ""), sso_auth.SsoError, "issuer"),
              "issuer 不匹配被拒"))
result.append(ok(expect_raises(lambda: sso_auth._verify_id_token(make_token({**base, "exp": now - timedelta(minutes=1)}), ""), sso_auth.SsoError, "expired"),
              "过期 token 被拒"))
other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
result.append(ok(expect_raises(lambda: sso_auth._verify_id_token(make_token({**base, "nonce": "n"}, other_key), "n"), sso_auth.SsoError, ""),
              "签名不对（冒充 IdP）被拒"))

settings_store.set_many({"sso.username_claim": "email"})
tok = make_token({**base, "email": "teacher@lab.local", "nonce": "n2"})
claims = sso_auth._verify_id_token(tok, "n2")
result.append(ok(sso_auth._username_from(claims) == "teacher@lab.local", "username_claim=email 生效"))
result.append(ok(expect_raises(lambda: sso_auth._verify_id_token(tok, "wrong-nonce"), sso_auth.SsoError, "nonce"),
              "nonce 不匹配被拒"))
settings_store.set_many({"sso.username_claim": "preferred_username"})

probe = sso_auth.probe()
result.append(ok(probe.get("ok") is True and probe.get("jwks_uri", "").endswith("/jwks"), "probe 回显 jwks_uri"))

row = auth.external_user("sso_alice", "u-1001", "sso", False)
result.append(ok(row["source"] == "sso" and row["password_hash"] == "!sso", "影子账号落库 source='sso'"))
again = auth.external_user("sso_alice", "u-1001", "sso", True)
result.append(ok(again["role"] == "admin", "重复登录 + 组升级 → 提权到 admin，账号不重复"))

methods = auth.auth_methods()
result.append(ok(methods["sso"]["enabled"] is True and methods["password"] is True, "/api/auth/methods 同时给出 password 与 sso"))

SERVER.shutdown()
print(f"\n{sum(result)}/{len(result)} PASS   临时目录：{TMP}")
sys.exit(0 if all(result) else 1)
