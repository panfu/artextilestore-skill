#!/usr/bin/env python3
"""artex — Artextile Odoo 業務查詢 CLI(供 Claude Code / ZCode / Codex 等 coding agent 使用)。

薄封裝:子命令直接映射 ar_chatbot.tools.odoo_tools.OdooSalesTools 的業務方法,
不重寫業務邏輯;能力與 chatbot 同源,隨上游演進。

身份模型(權限設計,2026-10 與 Fred 對齊):
- 預設(system):系統服務帳號,僅公共目錄類查詢(產品/庫存/色卡/品牌)。
- --as <客戶>:以該客戶的 Odoo portal session 執行。權限由兩層共同約束:
  Odoo 原生 record rules(只能讀自己的訂單/地址)+ 應用層
  PortalRestrictionChecker / restricted_customer_id(不能看原價折扣、
  只能給自己 partner 下單)。客戶視角操作必須走這條路,不得用系統帳號代理。
- --internal-view:內部視角查看客戶範圍數據(輸出帶警示頭,不得轉發給客戶)。

命令身份矩陣:
- 公共目錄:product / product-search / brand / sample-book / stock — 兩種身份皆可
- 客戶資料:orders / order / order-history / dye-lot / addresses / price-quote /
  carriers / small-order-check — 必須 --as 或 --internal-view 二選一
- 僅內部:customer(含內部客戶備註 x_studio_customer_remarks)— 僅系統身份

憑證治理:portal 憑證(--password / ARTEX_PORTAL_PASSWORD /
~/.config/artex/identities/<別名>.env 的 ARTEX_CREDENTIAL)僅在本次進程內
換取 session,不寫盤、不緩存。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

# ── 倉庫定位與環境引導 ─────────────────────────────────────────────

DEFAULT_CHATBOT_ROOT = Path.home() / "ar" / "chatbot"
DEFAULT_IDENTITIES_DIR = Path.home() / ".config" / "artex" / "identities"

PUBLIC_COMMANDS = {"product", "product-search", "brand", "sample-book", "stock"}
CUSTOMER_DATA_COMMANDS = {
    "orders",
    "order",
    "order-history",
    "dye-lot",
    "addresses",
    "price-quote",
    "carriers",
    "small-order-check",
}
INTERNAL_ONLY_COMMANDS = {"customer"}


def chatbot_root() -> Path:
    """Locate the ar_chatbot repo to import from.

    Resolution order: ARTEX_CHATBOT_ROOT env → the repo containing this script
    (skill is symlinked from ~/.agents/skills, __file__ resolves to the repo) →
    ~/ar/chatbot main checkout.
    """
    env_root = os.environ.get("ARTEX_CHATBOT_ROOT")
    if env_root:
        return Path(env_root)
    here = Path(__file__).resolve().parent
    if (here.parent.parent / "ar_chatbot").is_dir():
        return here.parent.parent
    return DEFAULT_CHATBOT_ROOT


def load_env_file(explicit: Optional[str] = None) -> Optional[Path]:
    """Load .env (ARTEX_ENV_FILE > explicit arg > repo .env > ~/ar/chatbot/.env).

    Never overrides already-set environment variables.
    """
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env_file = os.environ.get("ARTEX_ENV_FILE")
    if env_file:
        candidates.append(Path(env_file))
    root = chatbot_root()
    candidates.append(root / ".env")
    candidates.append(DEFAULT_CHATBOT_ROOT / ".env")

    for path in candidates:
        if path.is_file():
            from dotenv import load_dotenv

            load_dotenv(path, override=False)
            return path
    return None


def map_internal_creds() -> None:
    """get_odoo_env() reads ODOO_USER/ODOO_PASSWORD; chatbot .env may only
    define the ODOO_USER_INTERNAL/ODOO_INTERNAL_PASSWORD names (same fallback
    as the MCP server and gmail channel)."""
    if not os.environ.get("ODOO_USER"):
        internal = os.environ.get("ODOO_USER_INTERNAL", "")
        if internal:
            os.environ["ODOO_USER"] = internal
    if not os.environ.get("ODOO_PASSWORD"):
        internal = os.environ.get("ODOO_INTERNAL_PASSWORD", "")
        if internal:
            os.environ["ODOO_PASSWORD"] = internal


def import_ar_chatbot():
    """Import ar_chatbot lazily (pulls agno; keep `list`/usage fast)."""
    root = chatbot_root()
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    import ar_chatbot  # noqa: F401  (validates import path)

    return root


# ── API key(JSON-RPC service)模式客戶端 ────────────────────────────


class ApiKeyOdooClient:
    """Odoo client for API-key credentials via the /jsonrpc service layer.

    Odoo's /web/session/authenticate is interactive-only and rejects API
    keys by design; API keys only authenticate on the RPC service layer
    (common/object). This client mirrors the subset of the chatbot
    OdooClient surface used by OdooSalesTools, routing everything through
    /jsonrpc with the key attached to every call — no web session at all.

    Constructed via OdooSalesTools(odoo_client=...); duck-typed, so the
    toolkit methods work unchanged.
    """

    def __init__(self, url: str, db: str, login: str, api_key: str, verify_ssl: bool = False):
        from ar_chatbot.scripts.lib.odoo_client import OdooClient

        # Parent __init__ auto-logins when user/password are provided; an
        # API key would fail that web-session login, so pass no credentials
        # and set them afterwards.
        self._odoo = OdooClient(url=url, db=db, verify_ssl=verify_ssl)
        self._odoo.user = login
        self._odoo.password = api_key
        self.url = self._odoo.url
        self.db = self._odoo.db
        self.user = login
        self.api_key = api_key
        self.uid: Optional[int] = None

    def _jsonrpc(self, service: str, method: str, args: list) -> Any:
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "call",
                "params": {"service": service, "method": method, "args": args},
            }
        ).encode()
        req = urllib.request.Request(
            self._odoo._build_url("/jsonrpc"),
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": self._odoo._user_agent},
        )
        with self._odoo._opener.open(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        if "error" in data:
            err = data["error"].get("data", {})
            raise RuntimeError(
                "Odoo /jsonrpc error (%s): %s"
                % (data["error"].get("code"), err.get("message") or data["error"].get("message"))
            )
        return data.get("result")

    def authenticate(self) -> int:
        """common.authenticate with the API key; returns uid."""
        uid = self._jsonrpc("common", "authenticate", [self.db, self.user, self.api_key, {}])
        if not uid:
            raise SystemExit(
                "artex: API key 登入失敗(common.authenticate 返回空)。key 可能已被撤銷,請重跑 artex login。"
            )
        self.uid = uid
        self._odoo._uid = uid
        return uid

    # ── OdooClient surface used by OdooSalesTools ───────────────────

    def _call(self, model: str, method: str, args=None, kwargs=None) -> Any:
        return self._jsonrpc(
            "object",
            "execute_kw",
            [self.db, self.uid, self.api_key, model, method, args or [], kwargs or {}],
        )

    def __getattr__(self, name: str):
        """Delegate everything else to the wrapped OdooClient.

        OdooSalesTools calls high-level helpers (search_customer,
        search_orders, get_products_by_sample_book_number, ...). Those all
        funnel through OdooClient._call on the real client — but here they
        must run against /jsonrpc instead, so intercept _call-based helpers
        by swapping the delegate's _call to ours while the call runs.
        """

        attr = getattr(self._odoo, name, None)
        if attr is None:
            raise AttributeError(name)
        if callable(attr) and not name.startswith("_"):
            def wrapper(*a, **kw):
                original = self._odoo._call
                self._odoo._call = self._call
                try:
                    return attr(*a, **kw)
                finally:
                    self._odoo._call = original

            return wrapper
        return attr


# ── 身份層 ─────────────────────────────────────────────────────────


class PortalIdentity:
    def __init__(self, login: str, client, binding):
        self.login = login
        self.client = client
        self.binding = binding
        self.user_type = binding.user_type
        self.partner_id = binding.partner_id


class SystemIdentity:
    user_type = "internal"
    partner_id = None
    login = None


def identities_dir() -> Path:
    return Path(os.environ.get("ARTEX_IDENTITIES_DIR", DEFAULT_IDENTITIES_DIR))


def read_identity_file(alias: str) -> Optional[dict]:
    """Read ~/.config/artex/identities/<alias>.env → {login, credential}."""
    path = identities_dir() / f"{alias}.env"
    if not path.is_file():
        return None
    from dotenv import dotenv_values

    values = dotenv_values(path)
    login = values.get("ARTEX_LOGIN")
    cred = values.get("ARTEX_CREDENTIAL")
    if login and cred:
        return {"login": login, "credential": cred}
    return None


def resolve_customer_code_to_login(code: str) -> str:
    """客戶碼(如 E0056)/ partner_id → portal login(用系統帳號解析公共元數據)。"""
    from ar_chatbot.scripts.lib.odoo_client import OdooClient

    client = OdooClient(
        url=os.environ.get("ODOO_URL", ""),
        db=os.environ.get("ODOO_DB", ""),
        user=os.environ.get("ODOO_USER", ""),
        password=os.environ.get("ODOO_PASSWORD", ""),
    )
    if code.isdigit():
        partner_domain = [("id", "=", int(code))]
    else:
        partner_domain = [("code", "=", code.strip())]
    partners = client.execute_kw(
        "res.partner", "search_read", [partner_domain], {"fields": ["name"], "limit": 5}
    )
    if not partners:
        raise SystemExit(
            f"artex: 客戶碼 '{code}' 找不到對應 res.partner(可用 artex customer <名稱> 先確認)"
        )
    if len(partners) > 1:
        names = " / ".join(p["name"] for p in partners)
        raise SystemExit(f"artex: 客戶碼 '{code}' 匹配多個客戶:{names}。請改用 portal login。")
    users = client.execute_kw(
        "res.users",
        "search_read",
        [[("partner_id", "=", partners[0]["id"]), ("share", "=", True)]],
        {"fields": ["login"], "limit": 5},
    )
    if not users:
        raise SystemExit(
            f"artex: 客戶 {partners[0]['name']} 沒有 portal 帳號(share=True 的 res.users)。"
            "內部查看請用 --internal-view。"
        )
    if len(users) > 1:
        logins = " / ".join(u["login"] for u in users)
        raise SystemExit(f"artex: 客戶 {partners[0]['name']} 有多個 portal 帳號:{logins}。請指定 login。")
    return users[0]["login"]


def build_portal_identity(as_arg: str, password_arg: Optional[str]) -> PortalIdentity:
    """--as <login|客戶碼|identities 別名> → 真實 portal session + binding 替身。

    憑證優先級:--password > ARTEX_PORTAL_PASSWORD > identities 檔 ARTEX_CREDENTIAL。
    憑證只在本進程內換 session,不落盤。
    """
    from ar_chatbot.auth.models import OdooUserBinding
    from ar_chatbot.scripts.lib.odoo_client import OdooClient

    login: Optional[str] = None
    credential: Optional[str] = password_arg

    identity_file = read_identity_file(as_arg)
    if identity_file:
        login = identity_file["login"]
        credential = credential or identity_file["credential"]
    elif "@" in as_arg:
        login = as_arg
    else:
        login = resolve_customer_code_to_login(as_arg)

    if not credential:
        credential = os.environ.get("ARTEX_PORTAL_PASSWORD")
    if not credential:
        raise SystemExit(
            f"artex: 沒有 '{login}' 的憑證。三種供給方式:"
            f"(1) {identities_dir()}/{as_arg}.env 寫 ARTEX_LOGIN/ARTEX_CREDENTIAL;"
            "(2) 環境變量 ARTEX_PORTAL_PASSWORD;(3) --password(會進 shell 歷史,不建議)。"
        )

    verify_ssl = os.getenv("ODOO_VERIFY_SSL", "false").lower() == "true"

    if re.fullmatch(r"[0-9a-f]{40}", credential or ""):
        # API key(identities 由 artex login 生成):走 /jsonrpc service 層
        # (Odoo web session 端點設計上拒絕 API key)
        client = ApiKeyOdooClient(
            url=os.environ.get("ODOO_URL", ""),
            db=os.environ.get("ODOO_DB", ""),
            login=login,
            api_key=credential,
            verify_ssl=verify_ssl,
        )
        uid = client.authenticate()
        profile = client._jsonrpc("object", "execute_kw", [
            os.environ.get("ODOO_DB", ""), uid, credential,
            "res.users", "read", [[uid]], {
                "fields": ["name", "login", "partner_id", "share", "email"],
            },
        ])
        row = (profile or [{}])[0]
        partner = row.get("partner_id") or [None, ""]
        user_type = "portal" if row.get("share") else "internal"
        partner_id = partner[0]
        display_name = row.get("name")
        email = row.get("email")
    else:
        client = OdooClient(
            url=os.environ.get("ODOO_URL", ""),
            db=os.environ.get("ODOO_DB", ""),
            user=login,
            password=credential,
            verify_ssl=verify_ssl,
        )
        auth = client.authenticate_with_detail(login, credential)
        if not auth.success:
            if auth.requires_2fa or auth.error_type == "2fa":
                hint = "該帳號啟用了 2FA。請在 Odoo 為其生成 API key 後以 API key 作為憑證(artex login)。"
            elif auth.error_type == "credentials":
                hint = "憑證錯誤。請核對 identities 檔或重跑 artex login。"
            else:
                hint = auth.error or "未知錯誤"
            raise SystemExit(f"artex: portal 登入失敗({auth.error_type}):{hint}")
        uid = auth.uid
        prof = client.get_user_profile(uid) or {}
        user_type = prof.get("user_type", "portal")
        partner_id = prof.get("partner_id")
        display_name = prof.get("display_name")
        email = prof.get("email")

    binding = OdooUserBinding(
        # platform_type "cli":binding 僅在進程內用於應用層權限檢查,不進任何 DB
        platform_type="cli",
        platform_user_id=login,
        odoo_username=login,
        odoo_password_encrypted="",
        odoo_uid=uid,
        odoo_db=os.environ.get("ODOO_DB", ""),
        is_active=True,
        verification_status="verified",
        display_name=display_name,
        email=email,
        user_type=user_type,
        partner_id=partner_id,
    )
    if binding.user_type == "portal" and not binding.partner_id:
        raise SystemExit("artex: portal 帳號缺少 partner_id,拒絕執行(權限無法收斂)。")
    return PortalIdentity(login, client, binding)


def build_tools(identity):
    from ar_chatbot.tools.odoo_tools import OdooSalesTools

    if isinstance(identity, PortalIdentity):
        # 注入客戶 session + binding:Odoo 原生權限與應用層過濾同時生效
        return OdooSalesTools(odoo_client=identity.client, binding=identity.binding)
    return OdooSalesTools()


# ── 身份矩陣檢查 ───────────────────────────────────────────────────


def enforce_identity_matrix(args, identity) -> None:
    cmd = args.command
    is_portal = isinstance(identity, PortalIdentity)
    if is_portal and args.internal_view:
        raise SystemExit("artex: --internal-view 與 --as 互斥(內部視角請去掉 --as)。")
    if cmd in CUSTOMER_DATA_COMMANDS and not is_portal and not args.internal_view:
        raise SystemExit(
            f"artex: '{cmd}' 屬客戶資料查詢,不得用系統帳號執行。"
            "客戶視角請加 --as <客戶>;內部視角請顯式加 --internal-view(輸出僅供內部)。"
        )
    if cmd in INTERNAL_ONLY_COMMANDS and is_portal:
        raise SystemExit(
            "artex: 'customer' 含內部客戶備註,僅限系統身份(內部)執行,portal 身份拒絕。"
        )


def banner(identity, internal_view: bool, stream=None) -> None:
    stream = stream or sys.stderr
    db = os.environ.get("ODOO_DB", "?")
    url = os.environ.get("ODOO_URL", "")
    host = url.split("//")[-1].split("/")[0] if url else "?"
    if isinstance(identity, PortalIdentity):
        who = f"portal {identity.login} ({identity.user_type}, partner {identity.partner_id})"
    else:
        who = f"system {os.environ.get('ODOO_USER', '?')}"
    print(f"# env: {db} @ {host} | identity: {who}", file=stream)
    if internal_view:
        print(
            "# WARNING: internal view — 客戶範圍數據(含價格/折扣/備註),不得轉發給客戶",
            file=stream,
        )


# ── doctor / list ──────────────────────────────────────────────────

REQUIRED_ENV = ["ODOO_URL", "ODOO_DB"]


def cmd_doctor() -> int:
    env_file = os.environ.get("ARTEX_ENV_FILE_LOADED", "(未找到 .env)")
    print(f"chatbot root : {chatbot_root()}")
    print(f"env file     : {env_file}")
    print(f"identities   : {identities_dir()} ({len(list(identities_dir().glob('*.env')))} 個)")
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    missing_creds = not (os.environ.get("ODOO_USER") or os.environ.get("ODOO_USER_INTERNAL"))
    url = os.environ.get("ODOO_URL", "")
    print(f"ODOO_URL     : {url or '(未設置)'}")
    print(f"ODOO_DB      : {os.environ.get('ODOO_DB', '(未設置)')}")
    print(f"ODOO_USER    : {os.environ.get('ODOO_USER') or os.environ.get('ODOO_USER_INTERNAL', '') or '(未設置)'}")
    if missing or missing_creds:
        print(f"FAIL: 缺少環境變量 {missing + (['ODOO_USER(_INTERNAL)'] if missing_creds else [])}")
        return 2
    if not url:
        return 2

    import_ar_chatbot()
    from ar_chatbot.scripts.lib.odoo_client import OdooClient

    client = OdooClient(
        url=url,
        db=os.environ.get("ODOO_DB", ""),
        user=os.environ.get("ODOO_USER", ""),
        password=os.environ.get("ODOO_PASSWORD", ""),
    )
    auth = client.authenticate_with_detail(
        os.environ.get("ODOO_USER", ""), os.environ.get("ODOO_PASSWORD", "")
    )
    if auth.success:
        print(f"系統帳號登入 : OK (uid={auth.uid})")
        print("doctor: PASS")
        return 0
    if auth.error_type in {"network", "timeout", "ssl"}:
        print(
            f"FAIL: 連不上 Odoo({auth.error_type})。若為 Odoo.sh 構建輪換,用真實 DNS 驗證:\n"
            f'  curl -sS "https://1.1.1.1/dns-query?name={url.split("//")[-1].split("/")[0]}&type=A" '
            '-H "accept: application/dns-json"  # 返回 0.0.0.0 即構建已銷毀'
        )
    else:
        print(f"FAIL: 系統帳號登入失敗({auth.error_type}):{auth.error}")
    return 2


def cmd_list() -> int:
    print(__doc__)
    return 0


# ── login(設備授權流:artex → bridge → Odoo 一次確認) ─────────────


def _http_json(url: str, payload: Optional[dict] = None, method: str = "POST") -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise SystemExit(f"artex: bridge 請求失敗({e.code}):{body}") from e
    except urllib.error.URLError as e:
        raise SystemExit(f"artex: 連不上 bridge({url}):{e.reason}") from e


def cmd_login(args) -> int:
    """引導式登入:生成 bridge 授權連結 → 用戶在瀏覽器一次確認 → 取回 API key。"""
    bridge = (args.bridge or os.environ.get("ARTEX_BRIDGE_URL") or "").rstrip("/")
    if not bridge:
        raise SystemExit(
            "artex: 需要 --bridge 或 ARTEX_BRIDGE_URL(如 https://uat.artexbridge.com)。"
        )

    device = _http_json(f"{bridge}/api/artex/device", payload={})
    code, url, expires_in = device["code"], device["url"], int(device.get("expires_in", 600))
    print("在瀏覽器中打開以下連結完成授權(一次確認,無需向任何人提供密碼):")
    print(f"  {url}")
    if args.open and sys.platform == "darwin":
        os.system(f"open {url!r}")
    print("等待授權…(在終端按 Ctrl+C 取消)")

    deadline = time.time() + expires_in
    payload: Dict[str, Any] = {}
    while time.time() < deadline:
        time.sleep(2.5)
        result = _http_json(f"{bridge}/api/artex/device/poll", payload={"code": code})
        status = result.get("status")
        if status == "pending":
            continue
        if status == "authorized":
            payload = result["payload"]
            break
        raise SystemExit(f"artex: 授權未完成(status={status}),請重跑 artex login。")
    else:
        raise SystemExit("artex: 等待授權超時,請重跑 artex login。")

    login_name = payload.get("login", "")
    api_key = payload.get("api_key", "")
    alias = payload.get("customer_code") or login_name.split("@")[0] or "default"

    ident_dir = identities_dir()
    ident_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(ident_dir, 0o700)
    ident_file = ident_dir / f"{alias}.env"
    ident_file.write_text(f"ARTEX_LOGIN={login_name}\nARTEX_CREDENTIAL={api_key}\n")
    os.chmod(ident_file, 0o600)

    print(f"\n授權成功:{login_name}({payload.get('user_type')}, partner {payload.get('partner_id')})")
    print(f"憑證已寫入 {ident_file}(0600,僅本機)。")

    # 驗證:用拿到的 key 直連 Odoo 認一次
    import_ar_chatbot()
    client = ApiKeyOdooClient(
        url=os.environ.get("ODOO_URL", ""),
        db=os.environ.get("ODOO_DB", ""),
        login=login_name,
        api_key=api_key,
        verify_ssl=os.getenv("ODOO_VERIFY_SSL", "false").lower() == "true",
    )
    uid = client.authenticate()
    print(f"已用 API key 直連 Odoo 驗證通過(uid={uid})。")
    print(f'試一條:artex --as {alias} orders')
    return 0


# ── 子命令表 ───────────────────────────────────────────────────────


def add_subcommands(sub) -> None:
    p = sub.add_parser("product", help="按 6 位產品編號查詢(公共)")
    p.add_argument("code")
    p.add_argument("--pattern", help="花型提示,如 素色/checks")
    p.add_argument("--width", type=float, help="客戶要求的門幅(cm)")

    p = sub.add_parser("product-search", help="按名稱搜索產品(公共)")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("brand", help="品牌/系列查詢(公共)")
    p.add_argument("name")

    p = sub.add_parser("sample-book", help="色卡冊內在庫產品(公共,4 位冊號)")
    p.add_argument("book")

    p = sub.add_parser("stock", help="在庫產品清單(公共)")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("customer", help="按名稱搜索客戶(僅內部;含內部備註)")
    p.add_argument("name")

    p = sub.add_parser("orders", help="客戶訂單列表(客戶資料)")
    p.add_argument("customer", nargs="?", default="self", help="portal 身份可省略")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--state", help="draft/sent/sale/done/cancel")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("order", help="訂單詳情(客戶資料;訂單號如 SO00123)")
    p.add_argument("order_ref")

    p = sub.add_parser("order-history", help="歷史訂單行(客戶資料;portal 自動限本人)")
    p.add_argument("--customer", help="internal 視角必須給")
    p.add_argument("--product", help="6 位產品編號")
    p.add_argument("--days", type=int, default=180)

    p = sub.add_parser("dye-lot", help="染缸/批次資訊(客戶資料;portal 自動限本人)")
    p.add_argument("code")
    p.add_argument("--customer")
    p.add_argument("--days", type=int, default=365)

    p = sub.add_parser("addresses", help="客戶收貨地址(客戶資料)")
    p.add_argument("customer", nargs="?", default="self")

    p = sub.add_parser("price-quote", help="客戶報價(客戶資料;portal 只見最終價)")
    p.add_argument("customer", nargs="?", default="self")
    p.add_argument("spec", help='產品規格,如 "660302 1600 5Y"')

    p = sub.add_parser("carriers", help="物流商與運費(客戶資料;portal 釘死本人)")
    p.add_argument("customer", nargs="?", default="self")
    p.add_argument("--order", help="訂單號,給出則估算運費")

    p = sub.add_parser("small-order-check", help="小單檢測(客戶資料)")
    p.add_argument("order_ref")
    p.add_argument("code")

    sub.add_parser("doctor", help="環境自檢(env/憑證/連通性)")
    sub.add_parser("list", help="列印能力與身份矩陣")

    p = sub.add_parser(
        "login",
        help="引導式登入:瀏覽器一次確認後取得 API key(設備授權流,經 bridge)",
    )
    p.add_argument("--bridge", help="bridge 地址(默認 ARTEX_BRIDGE_URL,如 https://uat.artexbridge.com)")
    p.add_argument(
        "--no-open", dest="open", action="store_false",
        help="不自動打開瀏覽器(如 SSH 遠端)",
    )


def run_command(args, tools) -> str:
    cmd = args.command
    if cmd == "product":
        return tools.query_product(args.code, pattern_hint=args.pattern, customer_width_cm=args.width)
    if cmd == "product-search":
        return tools.search_products_by_name(args.name, limit=args.limit)
    if cmd == "brand":
        return tools.query_brand(args.name)
    if cmd == "sample-book":
        return tools.query_sample_book(args.book)
    if cmd == "stock":
        return tools.check_stock(limit=args.limit)
    if cmd == "customer":
        return tools.search_customer(args.name)
    if cmd == "orders":
        return tools.list_orders(args.customer, days=args.days, state=args.state, limit=args.limit)
    if cmd == "order":
        return tools.get_order_detail(args.order_ref)
    if cmd == "order-history":
        return tools.query_order_history(
            product_code=args.product, customer=args.customer, days=args.days
        )
    if cmd == "dye-lot":
        return tools.query_dye_lot_info(
            args.code, customer=args.customer, days=args.days
        )
    if cmd == "addresses":
        return tools.list_customer_addresses(args.customer)
    if cmd == "price-quote":
        return tools.get_price_quote(args.customer, args.spec)
    if cmd == "carriers":
        return tools.list_shipping_carriers(args.customer, order_ref=args.order)
    if cmd == "small-order-check":
        return tools.check_small_order(args.order_ref, args.code)
    raise SystemExit(f"artex: 未知命令 {cmd}(artex list 查看能力)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="artex",
        description="Artextile Odoo 業務查詢 CLI(基於 ar_chatbot 工具層;artex list 看能力)",
    )
    parser.add_argument(
        "--as",
        dest="as_who",
        metavar="CUSTOMER",
        help="以客戶身份執行:portal login / 客戶碼(E0056) / identities 別名",
    )
    parser.add_argument(
        "--password",
        help="portal 憑證(優先級最高;建議改用 ARTEX_PORTAL_PASSWORD 或 identities 檔)",
    )
    parser.add_argument(
        "--internal-view",
        action="store_true",
        help="內部視角查看客戶資料(與 --as 互斥;輸出帶警示,不得外發)",
    )
    parser.add_argument("--env-file", help="覆蓋 .env 路徑(默認 ARTEX_ENV_FILE 或 ~/ar/chatbot/.env)")
    sub = parser.add_subparsers(dest="command", required=True)
    add_subcommands(sub)

    args = parser.parse_args(argv)

    if args.command == "list":
        return cmd_list()

    env_path = load_env_file(args.env_file)
    if env_path:
        os.environ["ARTEX_ENV_FILE_LOADED"] = str(env_path)
    map_internal_creds()

    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "login":
        return cmd_login(args)

    import_ar_chatbot()
    identity = (
        build_portal_identity(args.as_who, args.password) if args.as_who else SystemIdentity()
    )
    enforce_identity_matrix(args, identity)
    tools = build_tools(identity)
    banner(identity, args.internal_view)
    print(run_command(args, tools))
    return 0


if __name__ == "__main__":
    sys.exit(main())
