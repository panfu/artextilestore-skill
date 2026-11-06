#!/usr/bin/env python3
"""artex — Artextile Odoo 業務查詢 CLI(供 Claude Code / ZCode / Codex 等 coding agent 使用)。

薄封裝:子命令直接映射 ar_chatbot.tools.odoo_tools.OdooSalesTools 的業務方法,
不重寫業務邏輯;能力與 chatbot 同源,隨上游演進。

身份模型(權限設計,2026-10 與 Fred 對齊;2026-11-06 收斂為純客戶面):
- 預設(system):系統服務帳號,僅公共目錄類查詢(產品/庫存/色卡/品牌)。
- --as <客戶>:以該客戶的 Odoo portal session 執行。權限由兩層共同約束:
  Odoo 原生 record rules(只能讀自己的訂單/地址)+ 應用層
  PortalRestrictionChecker / restricted_customer_id(不能看原價折扣、
  只能給自己 partner 下單)。客戶視角操作必須走這條路,不得用系統帳號代理。

命令身份矩陣:
- 公共目錄:product / product-search / brand / collections / bunch — 兩種身份皆可
  (術語:bunch 為 sample book/色卡冊的官方稱謂,一律用 bunch)
- 客戶資料:orders / order / order-history / dye-lot / addresses / price-quote /
  carriers / small-order-check — 必須 --as(portal 身份)

定位(2026-11-06 Fred 定):本 skill 面向客戶使用,內部辦公不用它——
無任何內部視角入口(--internal-view 已移除),客戶資料只能以客戶本人
portal 身份查(內部辦公走 bridge / oa_chat / odoo_app)。

憑證治理:portal 憑證(--password / ARTEX_PORTAL_PASSWORD /
~/.config/artex/identities/<別名>.env 的 ARTEX_CREDENTIAL / artex set
odoo_api_key 全局配置)僅在本次進程內換取 session,不寫盤、不緩存(config
與 identities 檔本身除外,均 0600)。

全局配置(artex set):odoo_base_url / odoo_login / odoo_api_key / odoo_db,
存 ~/.config/artex/config.env。odoo_base_url 一經設置即全局切換環境(壓過
.env 與 identities 檔內的環境坐標);odoo_login + odoo_api_key 供客戶快速
啟動 —— 配好後不帶 --as 直接執行,即以該 portal 身份運行。
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
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "artex"

# 生產默認坐標(artex set 未設置且無 .env 時的兜底;db 名取自生產構建,
# Odoo.sh 生產升級沿用同一數據庫,名稱不變)
DEFAULT_ODOO_BASE_URL = "https://www.artextilestore.com"
DEFAULT_ODOO_DB = "odoo-ps-pshk-artextile-international-main-4051457"

# artex set 設置項:CLI 名 → config.env 檔內鍵名
CONFIG_KEYS = {
    "odoo_base_url": "ODOO_BASE_URL",
    "odoo_login": "ODOO_LOGIN",
    "odoo_api_key": "ODOO_API_KEY",
    "odoo_db": "ODOO_DB",
}

PUBLIC_COMMANDS = {"product", "product-search", "brand", "collections", "bunch"}
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


def chatbot_root() -> Path:
    """Locate the ar_chatbot package to import from.

    Resolution order: ARTEX_CHATBOT_ROOT env → the repo containing this script
    (skill living inside the chatbot repo) → ~/ar/chatbot main checkout →
    the vendored subset shipped next to this script (self-contained installs
    on machines without the ~/ar workspace). Falls back to ~/ar/chatbot.
    """
    env_root = os.environ.get("ARTEX_CHATBOT_ROOT")
    if env_root:
        return Path(env_root)
    here = Path(__file__).resolve().parent
    if (here.parent.parent / "ar_chatbot").is_dir():
        return here.parent.parent
    if (DEFAULT_CHATBOT_ROOT / "ar_chatbot").is_dir():
        return DEFAULT_CHATBOT_ROOT
    vendored = here / "vendor"
    if (vendored / "ar_chatbot").is_dir():
        return vendored
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

        # Parent __init__ auto-logins when url/db/user/password all resolve
        # (falling back to ODOO_* env vars) — an API key would fail that
        # web-session login. Pass an empty db to skip the auto-login, then
        # restore it.
        self._odoo = OdooClient(url=url, db="", verify_ssl=verify_ssl)
        self._odoo.db = db
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
    def __init__(self, login, client, binding, odoo_url=None, odoo_db=None):
        self.login = login
        self.client = client
        self.binding = binding
        self.user_type = binding.user_type
        self.partner_id = binding.partner_id
        # environment actually used (identities coords win over .env) — banner
        self.odoo_url = odoo_url or os.environ.get("ODOO_URL", "")
        self.odoo_db = odoo_db or os.environ.get("ODOO_DB", "")


class SystemIdentity:
    user_type = "internal"
    partner_id = None
    login = None


def identities_dir() -> Path:
    return Path(os.environ.get("ARTEX_IDENTITIES_DIR", DEFAULT_IDENTITIES_DIR))


def read_identity_file(alias: str) -> Optional[dict]:
    """Read ~/.config/artex/identities/<alias>.env → {login, credential,
    odoo_url?, odoo_db?}. The odoo_* keys (written by `artex login`) make the
    identity self-sufficient on machines without a chatbot .env."""
    path = identities_dir() / f"{alias}.env"
    if not path.is_file():
        return None
    from dotenv import dotenv_values

    values = dotenv_values(path)
    login = values.get("ARTEX_LOGIN")
    cred = values.get("ARTEX_CREDENTIAL")
    if login and cred:
        return {
            "login": login,
            "credential": cred,
            "odoo_url": values.get("ARTEX_ODOO_URL"),
            "odoo_db": values.get("ARTEX_ODOO_DB"),
        }
    return None


# ── 全局配置層(artex set) ─────────────────────────────────────────


def config_file() -> Path:
    return Path(os.environ.get("ARTEX_CONFIG_FILE", DEFAULT_CONFIG_DIR / "config.env"))


def load_artex_config() -> Dict[str, str]:
    """Read ~/.config/artex/config.env → {odoo_base_url, odoo_login,
    odoo_api_key, odoo_db}(只含已設置且非空的項)。"""
    path = config_file()
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    values = dotenv_values(path)
    cfg: Dict[str, str] = {}
    for cli_name, file_key in CONFIG_KEYS.items():
        v = (values.get(file_key) or "").strip()
        if v:
            cfg[cli_name] = v
    return cfg


def write_artex_config(cfg: Dict[str, str]) -> Path:
    """Rewrite config.env from a full {cli_name: value} dict (missing keys
    are dropped → unset). Dir 0700 / file 0600, same governance as identities.
    Only tightens directories we created (or the artex-owned default) —
    never chmods a pre-existing foreign parent like /tmp."""
    path = config_file()
    parent = path.parent
    created = not parent.is_dir()
    parent.mkdir(parents=True, exist_ok=True)
    if created or parent == DEFAULT_CONFIG_DIR:
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
    lines = [f"{CONFIG_KEYS[k]}={cfg[k]}" for k in CONFIG_KEYS if k in cfg]
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    return path


def mask_secret(value: str) -> str:
    if len(value) >= 10:
        return f"{value[:4]}…{value[-4:]}"
    return "****"


def infer_db_from_url(url: str) -> Optional[str]:
    """Odoo.sh 的 *.dev.odoo.com 構建,數據庫名即首段子域
    (如 main-artextile-uat-39657068.dev.odoo.com → main-artextile-uat-39657068)。"""
    host = (url or "").split("//")[-1].split("/")[0].split(":")[0]
    if host.endswith(".dev.odoo.com"):
        return host[: -len(".dev.odoo.com")]
    return None


def effective_odoo_url(cfg: Optional[Dict[str, str]] = None, identity_url: Optional[str] = None) -> str:
    """環境坐標優先級:artex set odoo_base_url > identities 檔坐標 >
    ODOO_URL 環境變量(.env)> 生產默認。"""
    cfg = cfg if cfg is not None else load_artex_config()
    return (
        cfg.get("odoo_base_url")
        or identity_url
        or os.environ.get("ODOO_URL", "")
        or DEFAULT_ODOO_BASE_URL
    )


def effective_odoo_db(
    cfg: Optional[Dict[str, str]] = None,
    url: Optional[str] = None,
    identity_db: Optional[str] = None,
) -> str:
    """db 優先級:artex set odoo_db > 按 URL 推斷(*.dev.odoo.com)>
    identities 檔坐標 > ODOO_DB 環境變量 > 生產默認(僅當 URL 為生產域)。"""
    cfg = cfg if cfg is not None else load_artex_config()
    url = url or effective_odoo_url(cfg)
    if cfg.get("odoo_db"):
        return cfg["odoo_db"]
    inferred = infer_db_from_url(url)
    if inferred:
        return inferred
    if identity_db:
        return identity_db
    env_db = os.environ.get("ODOO_DB", "")
    if env_db:
        return env_db
    host = (url or "").split("//")[-1].split("/")[0].split(":")[0]
    if host == "www.artextilestore.com":
        return DEFAULT_ODOO_DB
    return ""


def apply_config_overrides() -> Dict[str, str]:
    """把配置層解析結果注入 ODOO_URL/ODOO_DB 環境變量 —— 系統身份、doctor、
    客戶碼解析等所有既有 os.environ 讀取點零改動地跟隨全局切換。"""
    cfg = load_artex_config()
    url = effective_odoo_url(cfg)
    db = effective_odoo_db(cfg, url)
    if url:
        os.environ["ODOO_URL"] = url
    if db:
        os.environ["ODOO_DB"] = db
    return cfg


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
            f"artex: 客戶碼 '{code}' 找不到對應 res.partner(請核對客戶碼,或直接用 portal login)"
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
            "請改用該客戶的 portal login。"
        )
    if len(users) > 1:
        logins = " / ".join(u["login"] for u in users)
        raise SystemExit(f"artex: 客戶 {partners[0]['name']} 有多個 portal 帳號:{logins}。請指定 login。")
    return users[0]["login"]


def build_portal_identity(as_arg: str, password_arg: Optional[str]) -> PortalIdentity:
    """--as <login|客戶碼|identities 別名> → 真實 portal session + binding 替身。

    憑證優先級:--password > identities 檔 ARTEX_CREDENTIAL > ARTEX_PORTAL_PASSWORD
    > artex set odoo_api_key(config 來源一律走 API key 路徑,不作密碼嘗試)。
    憑證只在本進程內換 session,不落盤(config 與 identities 檔本身除外)。
    """
    from ar_chatbot.auth.models import OdooUserBinding
    from ar_chatbot.scripts.lib.odoo_client import OdooClient

    login: Optional[str] = None
    credential: Optional[str] = password_arg
    odoo_url_env: Optional[str] = None
    odoo_db_env: Optional[str] = None

    cfg = load_artex_config()

    identity_file = read_identity_file(as_arg)
    if identity_file:
        login = identity_file["login"]
        credential = credential or identity_file["credential"]
        # environment coordinates from the identities file win over any .env
        # (self-sufficient on machines without a chatbot checkout) — but a
        # config-level odoo_base_url still wins over them (global switch)
        odoo_url_env = identity_file.get("odoo_url")
        odoo_db_env = identity_file.get("odoo_db")
    elif "@" in as_arg:
        login = as_arg
    else:
        login = resolve_customer_code_to_login(as_arg)

    if not credential:
        credential = os.environ.get("ARTEX_PORTAL_PASSWORD")
    credential_from_config = False
    if not credential:
        credential = cfg.get("odoo_api_key")
        credential_from_config = bool(credential)
    if not credential:
        raise SystemExit(
            f"artex: 沒有 '{login}' 的憑證。四種供給方式:"
            f"(1) {identities_dir()}/{as_arg}.env 寫 ARTEX_LOGIN/ARTEX_CREDENTIAL;"
            "(2) 環境變量 ARTEX_PORTAL_PASSWORD;(3) artex set odoo_api_key(快速啟動);"
            "(4) --password(會進 shell 歷史,不建議)。"
        )

    verify_ssl = os.getenv("ODOO_VERIFY_SSL", "false").lower() == "true"

    # 環境坐標:artex set odoo_base_url 是全局開關,壓過 identities 檔與 .env
    odoo_url = effective_odoo_url(cfg, odoo_url_env)
    odoo_db = effective_odoo_db(cfg, odoo_url, odoo_db_env)

    if credential_from_config or re.fullmatch(r"[0-9a-f]{40}", credential or ""):
        # API key(identities 由 artex login 生成 / artex set odoo_api_key 配置):
        # 走 /jsonrpc service 層(Odoo web session 端點設計上拒絕 API key)
        client = ApiKeyOdooClient(
            url=odoo_url,
            db=odoo_db,
            login=login,
            api_key=credential,
            verify_ssl=verify_ssl,
        )
        uid = client.authenticate()
        profile = client._jsonrpc("object", "execute_kw", [
            odoo_db, uid, credential,
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
            url=odoo_url,
            db=odoo_db,
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
        odoo_db=odoo_db,
        is_active=True,
        verification_status="verified",
        display_name=display_name,
        email=email,
        user_type=user_type,
        partner_id=partner_id,
    )
    if binding.user_type == "portal" and not binding.partner_id:
        raise SystemExit("artex: portal 帳號缺少 partner_id,拒絕執行(權限無法收斂)。")
    return PortalIdentity(login, client, binding, odoo_url=odoo_url, odoo_db=odoo_db)


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
    if cmd in CUSTOMER_DATA_COMMANDS and not is_portal:
        raise SystemExit(
            f"artex: '{cmd}' 屬客戶資料查詢,不得用系統帳號執行。"
            "請加 --as <客戶> 以客戶本人身份查詢(本 skill 僅提供客戶視角)。"
        )


def banner(identity, stream=None) -> None:
    stream = stream or sys.stderr
    if isinstance(identity, PortalIdentity):
        db = identity.odoo_db or "?"
        url = identity.odoo_url
    else:
        db = os.environ.get("ODOO_DB", "?")
        url = os.environ.get("ODOO_URL", "")
    host = url.split("//")[-1].split("/")[0] if url else "?"
    if isinstance(identity, PortalIdentity):
        who = f"portal {identity.login} ({identity.user_type}, partner {identity.partner_id})"
    else:
        who = f"system {os.environ.get('ODOO_USER', '?')}"
    print(f"# env: {db} @ {host} | identity: {who}", file=stream)


# ── doctor / list ──────────────────────────────────────────────────

REQUIRED_ENV = ["ODOO_URL", "ODOO_DB"]


def cmd_doctor() -> int:
    env_file = os.environ.get("ARTEX_ENV_FILE_LOADED")
    print(f"chatbot root : {chatbot_root()}")
    print(f"env file     : {env_file or '(未找到 .env)'}")
    cfg = load_artex_config()
    cpath = config_file()
    print(f"config       : {cpath}" + ("" if cpath.is_file() else "(尚未創建)"))
    for key in CONFIG_KEYS:
        print(f"  {key:<14}= {_describe_config_value(key, cfg)}")
    idents = list(identities_dir().glob("*.env"))
    print(f"identities   : {identities_dir()} ({len(idents)} 個)")
    url = os.environ.get("ODOO_URL", "")
    has_creds = bool(os.environ.get("ODOO_USER") or os.environ.get("ODOO_USER_INTERNAL"))
    print(f"ODOO_URL     : {url or '(未設置)'}")
    print(f"ODOO_DB      : {os.environ.get('ODOO_DB', '(未設置)')}")
    print(f"ODOO_USER    : {os.environ.get('ODOO_USER') or os.environ.get('ODOO_USER_INTERNAL', '') or '(未設置)'}")
    if not has_creds:
        print(
            "客戶模式(無系統憑證):公共目錄查詢與 --as 客戶身份可用。\n"
            "快速啟動:artex set odoo_login <login> + artex set odoo_api_key <key>;"
            "或 artex login 綁定客戶身份。"
        )
        if not url:
            print("doctor: PASS (customer mode)")
            return 0

    if not has_creds or not url:
        print("FAIL: 缺少 ODOO_URL 或系統憑證(內部模式必需)。")
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


# ── set / unset(全局配置層) ───────────────────────────────────────


def _describe_config_value(key: str, cfg: Dict[str, str]) -> str:
    value = cfg.get(key)
    if value is None:
        if key == "odoo_base_url":
            return f"(未設置 → 默認 {DEFAULT_ODOO_BASE_URL})"
        if key == "odoo_db":
            return "(未設置 → 按URL推斷/環境)"
        return "(未設置)"
    return mask_secret(value) if key == "odoo_api_key" else value


def cmd_set(args) -> int:
    if not args.key:
        cfg = load_artex_config()
        path = config_file()
        print(f"配置文件:{path}" + ("" if path.is_file() else "(尚未創建)"))
        for key in CONFIG_KEYS:
            print(f"  {key:<14}= {_describe_config_value(key, cfg)}")
        url = effective_odoo_url(cfg)
        print(f"\n生效環境(按 config + 當前進程環境):URL={url} DB={effective_odoo_db(cfg, url) or '?'}")
        print("設置:artex set <key> <value>;清除:artex unset <key>")
        return 0
    if args.key not in CONFIG_KEYS:
        raise SystemExit(f"artex: 未知設置項 '{args.key}'。可用:{', '.join(CONFIG_KEYS)}")
    if args.value is None:
        cfg = load_artex_config()
        print(f"{args.key} = {_describe_config_value(args.key, cfg)}")
        return 0
    value = args.value.strip()
    if not value:
        raise SystemExit(f"artex: 值不能為空;清除請用 artex unset {args.key}")
    if args.key == "odoo_base_url":
        if not re.match(r"^https?://", value):
            raise SystemExit(f"artex: odoo_base_url 需以 http:// 或 https:// 開頭,得到:{value}")
        value = value.rstrip("/")
    if args.key == "odoo_api_key" and not re.fullmatch(r"[0-9a-f]{40}", value):
        print(
            "警告:該值不是 40 位十六進制 Odoo API key(仍已保存;若認證失敗請核對粘貼內容)。",
            file=sys.stderr,
        )
    cfg = load_artex_config()
    cfg[args.key] = value
    path = write_artex_config(cfg)
    shown = mask_secret(value) if args.key == "odoo_api_key" else value
    print(f"已設置 {args.key} = {shown}")
    print(f"配置文件:{path}(0600)")
    if args.key == "odoo_base_url":
        print(f"提示:全局生效 —— 之後所有命令(含既有 --as 身份)都指向 {shown}")
    if args.key == "odoo_api_key":
        print("提示:配合 artex set odoo_login <login> 後,直接運行命令即以該客戶身份執行")
    return 0


def cmd_unset(args) -> int:
    if args.key not in CONFIG_KEYS:
        raise SystemExit(f"artex: 未知設置項 '{args.key}'。可用:{', '.join(CONFIG_KEYS)}")
    cfg = load_artex_config()
    if args.key not in cfg:
        print(f"{args.key} 原本就未設置。")
        return 0
    del cfg[args.key]
    path = write_artex_config(cfg)
    print(f"已清除 {args.key}(配置文件:{path})")
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
    odoo_url = payload.get("odoo_url") or os.environ.get("ODOO_URL", "")
    odoo_db = payload.get("odoo_db") or os.environ.get("ODOO_DB", "")

    ident_dir = identities_dir()
    ident_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(ident_dir, 0o700)
    ident_file = ident_dir / f"{alias}.env"
    lines = [f"ARTEX_LOGIN={login_name}", f"ARTEX_CREDENTIAL={api_key}"]
    if odoo_url:
        lines.append(f"ARTEX_ODOO_URL={odoo_url}")
    if odoo_db:
        lines.append(f"ARTEX_ODOO_DB={odoo_db}")
    ident_file.write_text("\n".join(lines) + "\n")
    os.chmod(ident_file, 0o600)

    print(f"\n授權成功:{login_name}({payload.get('user_type')}, partner {payload.get('partner_id')})")
    print(f"憑證已寫入 {ident_file}(0600,僅本機)。")

    # 驗證:用拿到的 key 直連 Odoo 認一次(identities 自帶環境坐標,不依賴 .env)
    import_ar_chatbot()
    client = ApiKeyOdooClient(
        url=odoo_url,
        db=odoo_db,
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

    p = sub.add_parser(
        "collections",
        help="collections/系列搜索(公共;含品牌與 bunch,oa_chat 同定義)",
    )
    p.add_argument("name", help="系列名,如 8239 / Fantasia")
    p.add_argument("--exact", action="store_true", help="精確匹配(默認模糊)")
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("bunch", help="bunch(色卡冊)內在庫產品(公共,4 位冊號)")
    p.add_argument("book")

    p = sub.add_parser("orders", help="客戶訂單列表(客戶資料)")
    p.add_argument("customer", nargs="?", default="self", help="portal 身份可省略")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--state", help="draft/sent/sale/done/cancel")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("order", help="訂單詳情(客戶資料;訂單號如 SO00123)")
    p.add_argument("order_ref")

    p = sub.add_parser("order-history", help="歷史訂單行(客戶資料;portal 自動限本人)")
    p.add_argument("--product", help="6 位產品編號")
    p.add_argument("--days", type=int, default=180)

    p = sub.add_parser("dye-lot", help="染缸/批次資訊(客戶資料;portal 自動限本人)")
    p.add_argument("code")
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
        "set",
        help="設置全局配置:odoo_base_url / odoo_login / odoo_api_key / odoo_db",
    )
    p.add_argument("key", nargs="?", help="設置項;整體省略則顯示當前配置")
    p.add_argument("value", nargs="?", help="新值;給 key 不給 value 則只顯示該項當前值")

    p = sub.add_parser("unset", help="清除一個全局配置項")
    p.add_argument("key")

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
    if cmd == "collections":
        return tools.query_collections(args.name, exact=args.exact, limit=args.limit)
    if cmd == "bunch":
        return tools.query_sample_book(args.book)
    if cmd == "orders":
        return tools.list_orders(args.customer, days=args.days, state=args.state, limit=args.limit)
    if cmd == "order":
        return tools.get_order_detail(args.order_ref)
    if cmd == "order-history":
        return tools.query_order_history(product_code=args.product, days=args.days)
    if cmd == "dye-lot":
        return tools.query_dye_lot_info(args.code, days=args.days)
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
    parser.add_argument("--env-file", help="覆蓋 .env 路徑(默認 ARTEX_ENV_FILE 或 ~/ar/chatbot/.env)")
    sub = parser.add_subparsers(dest="command", required=True)
    add_subcommands(sub)

    args = parser.parse_args(argv)

    if args.command == "list":
        return cmd_list()
    if args.command == "set":
        return cmd_set(args)
    if args.command == "unset":
        return cmd_unset(args)

    env_path = load_env_file(args.env_file)
    if env_path:
        os.environ["ARTEX_ENV_FILE_LOADED"] = str(env_path)
    map_internal_creds()
    cfg = apply_config_overrides()

    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "login":
        return cmd_login(args)

    import_ar_chatbot()
    as_who = args.as_who
    if not as_who and cfg.get("odoo_login"):
        # 快速啟動:config 的 odoo_login 為默認身份(顯式 --as 優先)
        as_who = cfg["odoo_login"]
    identity = (
        build_portal_identity(as_who, args.password) if as_who else SystemIdentity()
    )
    enforce_identity_matrix(args, identity)
    tools = build_tools(identity)
    banner(identity)
    print(run_command(args, tools))
    return 0


if __name__ == "__main__":
    sys.exit(main())
