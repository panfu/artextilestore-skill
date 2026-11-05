"""
Odoo Sales Tools - Shared toolkit for all agents
===================================================
Provides tools for querying products, customers, and managing orders in Odoo.

Integrates with ConversationContextManager for entity tracking and pronoun resolution.

User-level Session Support:
- When binding_service is provided, uses user's Odoo session for API calls
- Falls back to global credentials if no binding/session available
"""

import os
import re
import functools
import logging
import tempfile
import uuid
from decimal import ROUND_CEILING, Decimal
from typing import Dict, List, Optional, TYPE_CHECKING
import json

from agno.tools.toolkit import Toolkit

from ar_chatbot.scripts.lib.odoo_client import normalize_odoo_base_url
from ar_chatbot.tools.cache import TTLCache
from ar_chatbot.tools.factory_code_match import (
    STATUS_ABSENT_COLORWAY,
    STATUS_AMBIGUOUS,
    STATUS_EXACT,
    STATUS_NORMALIZED,
    FactoryCodeMatch,
    FactoryProduct,
    article_of,
    classify_code_input,
    match_factory_code,
    normalized_key,
)

if TYPE_CHECKING:
    from ar_chatbot.conversation import ConversationContextManager
    from ar_chatbot.auth import BindingService
    from ar_chatbot.auth.models import OdooUserBinding
    from ar_chatbot.scripts.lib.odoo_client import OdooClient

logger = logging.getLogger(__name__)

# European countries constant for region-of-origin display logic
EUROPEAN_COUNTRIES = frozenset(
    {
        "Albania",
        "Andorra",
        "Armenia",
        "Azerbaijan",
        "Belarus",
        "Belgium",
        "Bosnia and Herzegovina",
        "Bulgaria",
        "Croatia",
        "Cyprus",
        "Czech Republic",
        "Czechia",
        "Denmark",
        "Estonia",
        "Finland",
        "France",
        "Georgia",
        "Germany",
        "Greece",
        "Hungary",
        "Iceland",
        "Ireland",
        "Italy",
        "Kosovo",
        "Latvia",
        "Liechtenstein",
        "Lithuania",
        "Luxembourg",
        "Malta",
        "Moldova",
        "Monaco",
        "Montenegro",
        "Netherlands",
        "North Macedonia",
        "Norway",
        "Poland",
        "Portugal",
        "Romania",
        "Russia",
        "San Marino",
        "Serbia",
        "Slovakia",
        "Slovenia",
        "Spain",
        "Sweden",
        "Switzerland",
        "Turkey",
        "Ukraine",
        "United Kingdom",
        "Vatican City",
    }
)

YARD_TO_METER = 0.9144
BULK_ORDER_THRESHOLD_METERS = 15.0


def _fmt_qty(qty: float) -> str:
    """Format a quantity for display: strip trailing zeros (2.0 -> '2', 2.2 -> '2.2')."""
    if qty == 0:
        return "0"
    # Round to 2 decimals to avoid float noise, then drop trailing .0
    rounded = round(qty, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


QTY_STEP = Decimal("0.05")


def _require_binding(func):
    """Channel-session gate for data-query tools (2026-09-27 policy:
    不绑定什么都不能查). In a channel context (binding_service present)
    every Odoo data query needs a bound account — unbound users get the
    standard binding prompt. No-op without a binding_service (CLI,
    scripts, tests keep legacy open access).
    """
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        error = self._unbound_channel_error()
        if error:
            return f"Error: {error}"
        return func(self, *args, **kwargs)

    return wrapper


def round_up_qty(value: float) -> float:
    """Round an order quantity UP to the next multiple of 0.05 (1.41 -> 1.45).

    Same business rule as the oa_chat PO split flow, but computed via Decimal
    so a quantity that is already an exact 0.05 multiple never rounds up again
    because of binary float error (e.g. 0.15 must stay 0.15).
    """
    steps = (Decimal(str(value)) / QTY_STEP).to_integral_value(rounding=ROUND_CEILING)
    return float(steps * QTY_STEP)


def get_odoo_env(tools_instance=None) -> Dict[str, str]:
    """Get Odoo environment variables.

    System account architecture: always returns global environment variables.
    User-specific credentials are no longer supported.

    Args:
        tools_instance: OdooSalesTools instance (optional, ignored)

    Returns:
        Environment variables dict for Odoo scripts
    """
    odoo_env = {}

    # System account architecture: always use global credentials
    env_mapping = {
        "ODOO_URL": "ODOO_URL",
        "ODOO_DB": "ODOO_DB",
        "ODOO_USER": "ODOO_USER",  # Used by odoo_client.py
        "ODOO_PASSWORD": "ODOO_PASSWORD",  # Used by odoo_client.py
        "ODOO_COMPANY_ID": "ODOO_COMPANY_ID",  # Pin sale.order company
        # Legacy names for backward compatibility
        "ODOO_USER_INTERNAL": "ODOO_USER",
        "ODOO_INTERNAL_PASSWORD": "ODOO_PASSWORD",
        "ODOO_USER_PORTAL": "ODOO_USER",
        "ODOO_PORTAL_PASSWORD": "ODOO_PASSWORD",
    }

    for script_key, env_key in env_mapping.items():
        value = os.getenv(env_key)
        if value:
            odoo_env[script_key] = value

    return odoo_env


class OdooSalesTools(Toolkit):
    """Odoo Sales Tools - Query products, customers, and manage orders.

    Integrates with ConversationContextManager to track entities across
    the conversation for pronoun resolution.

    User-level Session Support:
    - Pass binding_service and platform_type to enable user-specific sessions
    - Falls back to global credentials if no binding available
    """

    # TTL defaults (seconds) for in-memory cache
    _CACHE_TTL_STOCK = 300  # 5 minutes
    _CACHE_TTL_CUSTOMER = 900  # 15 minutes
    _CACHE_TTL_QUOTE = 300  # 5 minutes

    # Pattern aliases for mismatch detection (RFC v2 §3.4.3)
    # Maps normalized category → list of known aliases (EN + CN)
    # Used by _normalize_pattern() and _extract_pattern_from_name()
    PATTERN_ALIASES = {
        "plain": ["plain", "素色", "净色", "純色", "solid"],
        "checks": ["checks", "check", "格仔", "格子", "格纹", "plaid"],
        "stripe": ["stripe", "stripes", "striped", "條紋", "条纹", "间条"],
        "herringbone": ["herringbone", "人字紋", "人字纹"],
        "hounds tooth": ["hounds tooth", "houndstooth", "犬牙紋", "千鸟格"],
        "tartan": ["tartan", "蘇格蘭格"],
        "dot": ["dot", "dots", "dotted", "波點", "圆点"],
        "jacquard": ["jacquard", "提花"],
    }

    # Minimum Order Quantity per brand.
    # Key: lowercase brand name substring for matching.
    # Value: (min_qty, unit) where unit is 'm' (meters) or 'y' (yards).
    # Brands not listed default to (0.1, 'y') i.e. 0.1 yards.
    BRAND_MOQ = {
        "carnet": (0.5, "m"),  # 0.5 meters
        # Add more brand-specific MOQs here as needed.
    }
    DEFAULT_MOQ = (0.1, "yd")  # 0.1 yards

    # Class-level default so instances built without __init__ (test doubles
    # using __new__) see "no injected session" instead of AttributeError.
    _injected_client = None

    def _get_moq_for_product(self, product: dict) -> tuple:
        """Return (min_qty, unit, min_qty_m, min_qty_yd) for a product based on its brand."""
        brand_id = product.get("brand_id")
        brand_name = self._extract_region_name(brand_id) or ""
        brand_lower = brand_name.lower()

        moq_qty, moq_unit = self.DEFAULT_MOQ
        for key, val in self.BRAND_MOQ.items():
            if key in brand_lower:
                moq_qty, moq_unit = val
                break

        # Convert to both units for threshold comparison
        if moq_unit == "m":
            min_qty_m = moq_qty
            min_qty_yd = moq_qty / 0.9144
        else:  # yards
            min_qty_yd = moq_qty
            min_qty_m = moq_qty * 0.9144

        return moq_qty, moq_unit, min_qty_m, min_qty_yd

    def __init__(
        self,
        context_manager: Optional["ConversationContextManager"] = None,
        binding_service: Optional["BindingService"] = None,
        platform_type: str = "lark",
        platform_user_id: Optional[str] = None,
        session_manager=None,
        odoo_client: Optional["OdooClient"] = None,
        binding: Optional["OdooUserBinding"] = None,
        *args,
        **kwargs,
    ):
        """Initialize OdooSalesTools.

        Args:
            context_manager: Optional ConversationContextManager for entity tracking
            binding_service: Optional BindingService for user-level Odoo sessions
            platform_type: Platform type ('lark' or 'wechat') for binding lookups
            platform_user_id: Platform user ID for binding lookups
            session_manager: Optional SessionManager for auto session splitting
            odoo_client: Optional externally-owned OdooClient whose session every
                tool call must use (e.g. a portal user session, injected by the
                CLI skill). None keeps the system-account behaviour.
            binding: Optional pre-resolved binding; skips the binding_service DB
                lookup in get_binding(). Pair with odoo_client so application-
                layer portal restrictions (restricted_customer_id, permission
                checker) apply on top of the injected session.
        """
        self.context_manager = context_manager
        self.binding_service = binding_service
        # Ensure platform_type is stored as string, not enum
        # Handle both enum and string inputs
        if hasattr(platform_type, "value"):
            self.platform_type = platform_type.value
        else:
            self.platform_type = str(platform_type) if platform_type else "lark"
        self.platform_user_id = platform_user_id
        self.session_manager = session_manager
        self._user_odoo_client = None  # Cached user OdooClient
        self._injected_client = odoo_client  # External session override; None → system account
        self._binding = binding  # Pre-set binding; None → resolve via binding_service
        self._cache = TTLCache()  # In-memory TTL cache for read operations
        self._last_product_name = (
            None  # Last queried product name (for pattern mismatch)
        )
        self._last_product_width_cm = None  # Last queried width (門幅 mismatch)

        super().__init__(
            tools=[
                self.query_sample_book,
                self.query_product,
                self.query_brand,
                self.search_products_by_name,
                self.check_stock,
                self.search_customer,
                self.get_product_link,
                self.create_order,
                self.get_price_quote,
                self.add_product_to_order,
                self.verify_order,
                # confirm_order removed - orders stay in quotation stage
                self.cancel_order,
                self.cancel_order_lines,
                self.update_order_line_qty,
                self.set_order_line_customer_po,
                self.list_customer_addresses,
                self.set_order_address,
                self.list_orders,
                self.get_order_detail,
                # get_user_defaults removed 2026-08-17: its bridge backend did a
                # 6-month order analysis that timed out and blocked the order
                # flow; Odoo inherits partner defaults (payment term etc.) at
                # order creation anyway.
                self.query_order_history,
                self.check_small_order,
                self.create_small_order_lark_task,
                self.add_order_note,
                self.create_customer_address,
                self.list_recent_orders_for_selection,
                self.query_dye_lot_info,
                self.list_shipping_carriers,
                self.calculate_shipping,
                self.update_order_shipping,
                self.submit_quotation_for_confirmation,
                self.submit_draft_reply_to_user,
                # generate_product_labels deliberately NOT registered here:
                # 牌仔 is 客服-only (AI mode), registered on demand via
                # enable_product_labels() — customer-facing agents must not
                # even see the tool (2026-10-07).
            ],
            *args,
            **kwargs,
        )

    def enable_product_labels(self) -> None:
        """Register the 牌仔 tool (客服 AI-mode only, 2026-10-07).

        Product labels are printed by 客服 on request and stay invisible to
        customer-facing agents. The WeChat AI-mode path (Chatwoot
        ``manual_ai_trigger``) calls this on its adapter's toolkit before
        each run; agno enumerates toolkit functions per run, so the late
        registration reaches the model on the same run. The AI-mode adapter
        is cached under a session key that never collides with the customer
        flow's, so this cannot leak into customer conversations. Idempotent.
        """
        if "generate_product_labels" not in self.functions:
            self.register(self.generate_product_labels)

    def get_user_odoo_client(self):
        """Get the user's OdooClient.

        System account architecture: all operations use system client.
        This method always returns None.

        Returns:
            Always None
        """
        return None

    def get_binding(self) -> Optional["OdooUserBinding"]:
        """Get the current user's binding.

        Returns:
            OdooUserBinding if bound, None otherwise
        """
        if (
            getattr(self, "_binding", None) is None
            and getattr(self, "binding_service", None)
            and self.platform_user_id
        ):
            self._binding = self.binding_service.db.get_binding(
                self.platform_type, self.platform_user_id
            )
        return getattr(self, "_binding", None)

    def get_permission_checker(self):
        """Get a permission checker for the current user.

        Returns:
            PortalRestrictionChecker instance
        """
        from ar_chatbot.auth.permission_checker import PortalRestrictionChecker

        return PortalRestrictionChecker(self.get_binding())

    def is_portal_user(self) -> bool:
        """Check if current user is a portal user.

        Returns:
            True if portal user, False otherwise
        """
        binding = self.get_binding()
        return binding and binding.user_type == "portal"

    def get_restricted_customer_id(self) -> Optional[int]:
        """Get the customer ID that portal user is restricted to.

        For portal users, returns their partner_id (they can only access this).
        For internal users, returns None (no restriction).

        Returns:
            Customer ID if portal user, None if internal user
        """
        binding = self.get_binding()
        if binding and binding.user_type == "portal":
            return binding.partner_id
        return None

    def _resolve_partner_for_binding(
        self, customer: Optional[str]
    ) -> tuple[Optional[int], Optional[str]]:
        """Resolve a customer string to (partner_id, error_message).

        Portal semantics (same pin-to-own-partner convention as
        list_customer_addresses): pricing/ordering is ALWAYS own-account,
        so an unresolvable string — LLM passed a polluted nickname, an
        email, or a display-name+nickname merge — falls back to the bound
        partner instead of dead-ending the quote/order. Symptom before
        this fallback: portal quotes failed with「暫時無法獲取報價信息」
        whenever the guard's search came back empty (2026-10-06). A string
        that resolves to a FOREIGN partner is rejected — name-based
        requests bypass the numeric permission check in create_order.

        Internal users keep the strict behaviour: unresolvable -> error.
        """
        customer_str = str(customer).strip() if customer else ""

        requested_partner_id: Optional[int] = None
        if customer_str.isdigit():
            requested_partner_id = int(customer_str)
        elif customer_str:
            customers = self._get_system_client().search_customer(customer_str, limit=1)
            requested_partner_id = customers[0]["id"] if customers else None

        restricted_customer_id = self.get_restricted_customer_id()
        if self.is_portal_user():
            if not restricted_customer_id:
                return None, (
                    "Error: Cannot determine your customer account. "
                    "Please contact support."
                )
            if requested_partner_id is None:
                logger.info(
                    "[OdooTools] portal customer fallback: unresolvable "
                    f"{customer_str[:40]!r} -> own partner {restricted_customer_id}"
                )
                return restricted_customer_id, None
            if requested_partner_id != restricted_customer_id:
                return None, (
                    "出於安全考慮，portal 用戶只能操作自己帳戶"
                    f"（Customer ID: {restricted_customer_id}），"
                    f"無法為「{customer_str}」對應的客戶辦理。"
                    "請向客戶說明須由業務員協助。"
                )
            return requested_partner_id, None

        if requested_partner_id is None:
            return None, f"\nCustomer not found: {customer}"
        return requested_partner_id, None

    def has_user_session(self) -> bool:
        """Check if user has a valid Odoo session.

        System account architecture: always returns False.
        All Odoo API calls use the system account.

        Returns:
            Always False
        """
        return False

    def _resolve_order_ref(self, order_ref: str) -> tuple[int, str] | tuple[None, str]:
        """Resolve order_ref (name or numeric ID) to (order_id, order_name).

        Resolution order:
        1. Exact name match (e.g. '26050207', 'SO00123')
        2. Numeric ID lookup
        3. Customer PO search: client_order_ref on sale.order header
        4. Customer PO search: customer_po on sale.order.line

        Every resolved order passes _check_portal_order_access (portal users
        only reach their own partner's orders; unbound channel users are
        denied up front — incident 2026-09-27: a portal-bound WeChat customer
        queried another customer's order by number and got the full detail
        back). Portal PO searches are partner-scoped so a shared PO text
        can't resolve to another customer's order.

        Returns (order_id, order_name) on success, or (None, error_message) on failure.
        """
        client = self._get_system_client()
        order_ref_str = str(order_ref).strip()

        unbound_error = self._unbound_channel_error()
        if unbound_error:
            return None, unbound_error

        restricted_partner_id = self.get_restricted_customer_id()

        # 1. Try to find order by exact name match
        orders = client._call(
            model="sale.order",
            method="search_read",
            args=[[("name", "=", order_ref_str)]],
            kwargs={"fields": ["id", "name", "state"]},
        )
        if orders:
            access_error = self._check_portal_order_access(orders[0]["id"])
            if access_error:
                return None, access_error
            return orders[0]["id"], orders[0].get("name", order_ref_str)

        # 2. Try as numeric ID
        try:
            order_id = int(order_ref_str)
            order = client.get_order(order_id)
            if order:
                access_error = self._check_portal_order_access(order_id)
                if access_error:
                    return None, access_error
                return order_id, order.get("name", f"#{order_id}")
        except (ValueError, TypeError):
            pass

        # 3. Search by client_order_ref (Customer PO on order header)
        po_domain: list = [("client_order_ref", "=", order_ref_str)]
        if restricted_partner_id:
            po_domain.append(("partner_id", "=", restricted_partner_id))
        orders = client._call(
            model="sale.order",
            method="search_read",
            args=[po_domain],
            kwargs={"fields": ["id", "name", "state", "client_order_ref"], "limit": 5},
        )
        if orders:
            o = orders[0]
            logger.info(
                f"[OdooTools] _resolve_order_ref: found via client_order_ref: {o['name']}"
            )
            return o["id"], o.get("name", order_ref_str)

        # 4. Search by customer_po on order lines
        line_po_domain: list = [("customer_po", "=", order_ref_str)]
        if restricted_partner_id:
            line_po_domain.append(("order_id.partner_id", "=", restricted_partner_id))
        lines = client._call(
            model="sale.order.line",
            method="search_read",
            args=[line_po_domain],
            kwargs={"fields": ["order_id"], "limit": 5},
        )
        if lines:
            order_id_raw = lines[0].get("order_id")
            if isinstance(order_id_raw, list) and len(order_id_raw) > 0:
                line_order_id = order_id_raw[0]
            else:
                line_order_id = int(order_id_raw)
            order = client.get_order(line_order_id)
            if order:
                logger.info(
                    f"[OdooTools] _resolve_order_ref: found via line customer_po: {order['name']}"
                )
                return line_order_id, order.get("name", order_ref_str)

        return None, f"Order '{order_ref}' not found"

    def _unbound_channel_error(self) -> Optional[str]:
        """Denial message for order/customer-scoped tools used without a
        binding in a channel session (binding_service present but no binding
        row). Mirrors create_order's unbound policy. Contexts without a
        binding_service (CLI, scripts, tests) keep legacy open access —
        there is no identity to check.
        """
        if (
            getattr(self, "binding_service", None) is not None
            and not self.get_binding()
        ):
            return (
                "Permission denied: No Odoo account is bound. Please bind "
                'your account first (say "我要綁定" or "bind account" to get '
                "a binding link)."
            )
        return None

    def _check_portal_order_access(self, order_id: int) -> Optional[str]:
        """Check if a portal user has access to a specific order.

        Returns None if access is allowed, or an error string if denied.
        Non-portal users always have access (returns None).

        Args:
            order_id: The sale order ID to check

        Returns:
            None if access allowed, error string if denied
        """
        if not self.is_portal_user():
            return None

        restricted_partner_id = self.get_restricted_customer_id()
        if not restricted_partner_id:
            return (
                "Error: Cannot determine your customer account. Please contact support."
            )

        try:
            client = self._get_system_client()
            order = client._call(
                model="sale.order",
                method="read",
                args=[[order_id]],
                kwargs={"fields": ["partner_id"]},
            )
            if not order:
                return f"Order {order_id} not found"

            order_partner_id = order[0].get("partner_id")
            if isinstance(order_partner_id, list):
                order_partner_id = order_partner_id[0]

            if order_partner_id != restricted_partner_id:
                return "Permission denied: This order does not belong to your account."

            return None
        except Exception as e:
            logger.error(f"[OdooTools] _check_portal_order_access error: {e}")
            return f"Error checking order access: {str(e)}"

    def _get_system_client(self):
        """Get a system-level OdooClient using global credentials.

        This creates a client using the global ODOO_URL/ODOO_DB/ODOO_USER/ODOO_PASSWORD
        environment variables. Used for operations that don't require user-specific sessions.

        Returns:
            OdooClient instance
        """
        if self._injected_client is not None:
            # External session (e.g. CLI skill --as <customer>): every call runs
            # under that identity so Odoo-native permissions apply directly.
            return self._injected_client

        from ar_chatbot.scripts.lib.odoo_client import OdooClient

        env = get_odoo_env(self)
        return OdooClient(
            url=env.get("ODOO_URL"),
            db=env.get("ODOO_DB"),
            user=env.get("ODOO_USER"),
            password=env.get("ODOO_PASSWORD"),
        )

    @staticmethod
    def _sanitize_product_code(product_code) -> str:
        """Normalize product code input.

        Preprocessing steps (in order):
        1. Strip "AR" prefix (case-insensitive) — e.g. "AR227707" → "227707"
        2. Remove non-digit characters (handles space-separated "2277 07" → "227707",
           dashes, slashes, etc.)
        3. Return first 6 digits if ≥6 digits found; otherwise return original stripped input.

        Factory-number inputs (contains "." / "/" / letters, or 5 pure digits —
        e.g. "499.893/2") are returned as-is so the downstream lookup FAILS
        loudly ("not found") instead of silently matching an unrelated product
        via digit-stripping. Callers that support factory numbers resolve them
        via the factory-code matcher BEFORE reaching this point.
        """
        raw = str(product_code)
        if classify_code_input(raw) == "factory_code":
            return raw.strip()
        code = re.sub(r"^AR", "", raw, flags=re.IGNORECASE)
        digits = re.sub(r"[^0-9]", "", code)
        return digits[:6] if len(digits) >= 6 else raw.strip()

    # ==================== Factory-Code (布廠號) Resolution ====================

    _CACHE_TTL_FACTORY = 900  # 15 minutes — factory-code → product mapping

    def _match_factory_input(self, raw_code: str) -> FactoryCodeMatch:
        """Factory-number → product match, cached (mapping changes rarely)."""
        cache_key = f"factory:{normalized_key(raw_code)}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        client = self._get_system_client()
        match = match_factory_code(client, raw_code)
        self._cache.set(cache_key, match, self._CACHE_TTL_FACTORY)
        return match

    def _factory_product_url(self, product: FactoryProduct) -> str:
        """Shop URL for a factory-matched product (get_product_link's format)."""
        base_url = normalize_odoo_base_url(
            os.environ.get("ODOO_URL") or "https://www.artextilestore.com"
        )
        return f"{base_url}/shop/{product.code}-{product.template_id}"

    def _resolve_factory_spec_code(
        self, raw_code: str
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Resolve a factory-number spec code to the 6-digit product code.

        Returns (ar_code, notice, error): exactly one of ar_code / error is
        set. Used by get_price_quote / add_product_to_order — a factory number
        must resolve BEFORE any product lookup, never via digit-stripping.
        """
        match = self._match_factory_input(raw_code)
        if match.status in (STATUS_EXACT, STATUS_NORMALIZED) and match.products:
            product = match.products[0]
            notice = (
                f"[FACTORY_CODE_MATCH] 工廠號 \"{match.input}\" 命中供應商編號 "
                f"{'、'.join(match.hit_codes)} → 產品 {product.code}（{product.name}）。"
                "此映射為系統自動匹配，回覆客戶時請先複述產品編號與名稱，並附產品鏈接 "
                f"{self._factory_product_url(product)} 供客戶核對確認後再報價/下單。"
            )
            return product.code, notice, None

        if match.status == STATUS_AMBIGUOUS and match.products:
            lines = [
                (
                    f"[FACTORY_CODE_MATCH] 工廠號 \"{match.input}\" 命中多個產品，"
                    "請向客戶確認是哪一款："
                )
            ]
            for i, product in enumerate(match.products, 1):
                lines.append(
                    f"{i}. {product.code} - {product.name}："
                    f"{self._factory_product_url(product)}"
                )
            lines.append("客戶確認後請改用該六位產品編號重新報價/下單。")
            return None, None, "\n".join(lines)

        if match.status == STATUS_AMBIGUOUS:
            detail = "編號在庫但無法對應到可售產品，請轉人工跟進。"
        elif match.status == STATUS_ABSENT_COLORWAY:
            detail = (
                f"布號 {article_of(match.input)} 在庫但色號未收錄（色號可能有誤），"
                "請與客戶核對色號或轉人工跟進。"
            )
        else:
            detail = "未在供應商工廠號庫中找到該編號。"
        error = (
            f"[FACTORY_CODE_UNRESOLVED] \"{match.input}\" 不是六位產品編號，"
            f"{detail}\n請向客戶索取六位產品編號（如 227707），或轉人工跟進。"
        )
        return None, None, error

    def _query_product_by_factory_code(self, raw_code: str) -> str:
        """query_product branch for factory-number inputs: match → product info.

        Never digit-strips the input. Unique match returns the resolved
        product's full info (with a confirmation notice); ambiguous lists the
        candidates; absent asks for the 6-digit code.
        """
        ar_code, notice, error = self._resolve_factory_spec_code(raw_code)
        if error:
            return error
        result = self._query_product_via_script(ar_code)
        return f"{notice}\n\n{result}"

    # ==================== Pattern Mismatch Detection ====================

    def _normalize_pattern(self, text: str) -> Optional[str]:
        """Normalize a pattern description to a canonical category.

        Searches all PATTERN_ALIASES aliases using word-boundary matching.
        Returns the matched category name (e.g. "plain"), or None.

        Note: Python ``\\b`` works correctly for CJK characters because CJK chars
        are in ``\\w``, so ``\\b`` fires at CJK/space or CJK/ASCII boundaries.
        """
        if not text:
            return None
        text_lower = text.lower().strip()
        for category, aliases in self.PATTERN_ALIASES.items():
            for alias in aliases:
                # Word-boundary matching to avoid "check" matching "checked".
                # \b works correctly for CJK too (CJK chars are \w).
                pattern = r"\b" + re.escape(alias) + r"\b"
                if re.search(pattern, text_lower):
                    return category
        return None

    def _extract_pattern_from_name(self, product_name: str) -> Optional[str]:
        """Extract pattern category from a product name.

        Uses _normalize_pattern() with all PATTERN_ALIASES aliases.
        """
        return self._normalize_pattern(product_name)

    def _check_pattern_mismatch(
        self, pattern_hint: str, product_name: str
    ) -> Optional[str]:
        """Check if user-mentioned pattern matches the product's actual pattern.

        Args:
            pattern_hint: Pattern description from user (e.g. "checks", "素色")
            product_name: Product name from Odoo (e.g. "227707 Plain")

        Returns:
            Mismatch warning string, or None if matched / no hint / unrecognized.
        """
        if not pattern_hint or not product_name:
            return None

        hint_category = self._normalize_pattern(pattern_hint)
        if not hint_category:
            # User's pattern not in known mapping — skip detection (conservative)
            return None

        actual_category = self._extract_pattern_from_name(product_name)
        if not actual_category:
            # product.name has no recognizable pattern keyword — skip detection
            return None

        if hint_category != actual_category:
            return (
                f"\u26a0\ufe0f PATTERN MISMATCH WARNING:\n"
                f'User mentioned pattern: "{pattern_hint}" (\u2192 {hint_category})\n'
                f'Actual product pattern: "{actual_category}" (from "{product_name}")\n'
                f"Please confirm with the customer that this is the correct product."
            )

        return None

    def _check_width_mismatch(self, customer_width_cm, product: dict):
        """Build a WIDTH MISMATCH warning when the customer-stated fabric
        width (門幅/宽幅 XXcm) differs from the system record by more than
        OdooClient.WIDTH_MISMATCH_TOLERANCE_CM.

        Warns only — the line is still added; the model must relay the
        difference as a NOTICE (no width question, no extra interaction —
        one reply carries ONE interaction, the flow's own price/order
        confirm).

        Args:
            customer_width_cm: width in cm as stated by the customer
            product: Product data returned from Odoo (needs 'width_cm')

        Returns:
            Warning string, or None if no stated width / within tolerance.
        """
        if customer_width_cm is None:
            return None

        from ar_chatbot.scripts.lib.odoo_client import OdooClient as OdooClientLib

        info = OdooClientLib.check_width_mismatch(customer_width_cm, product)
        if not info["is_mismatch"]:
            return None

        return (
            "\u26a0\ufe0f WIDTH MISMATCH WARNING:\n"
            f"Customer stated width: {info['customer_width_cm']:g}cm\n"
            f"This product's width: {info['system_width_cm']:g}cm "
            f"(diff: {info['diff_cm']:g}cm)\n"
            "Mention this as a NOTICE: the product's own width spec vs what "
            "the customer said (e.g. ⚠️ 此款門幅為 148cm，與您說的 150cm "
            "相差 2cm) — never \"system record\" wording, never a width "
            "question; the flow's own price/order confirmation stays the "
            "single interaction."
        )

    @staticmethod
    def _collect_order_soft_triggers(
        stock_info: dict, quantity_meters: float
    ) -> list[str]:
        """Return runtime soft triggers for order-stage flows."""
        triggers: list[str] = []
        if stock_info.get("status") == "low_stock":
            triggers.append("low_stock")
        if quantity_meters >= BULK_ORDER_THRESHOLD_METERS:
            triggers.append("bulk_order")
        return triggers

    @staticmethod
    def _append_soft_triggers(output: str, triggers: list[str]) -> str:
        """Append runtime soft trigger tags to a tool result."""
        if not triggers:
            return output
        return f"{output}\n[SOFT_TRIGGERS:{','.join(triggers)}]"

    @staticmethod
    def _unorderable_stock_error(stock_info: dict) -> str:
        """Error line for unorderable products, tagged with the stock status.

        The [STOCK:...] tag tells the agent exactly WHICH wording to use
        (sold_out = 缺貨 temporary, dropped = 停產 discontinued) so it never
        guesses between them.
        """
        bits = [stock_info.get("status") or "unknown"]
        if stock_info.get("restock_status"):
            bits.append(stock_info["restock_status"])
        return f"\n[ERROR] [STOCK:{'+'.join(bits)}] {stock_info['guidance_message']}"

    @staticmethod
    def _detect_uom(uom_name: str) -> tuple:
        """Detect whether UOM is meters or yards.

        Uses the canonical keyword-matching pattern from the codebase:
        - meter keywords: 'meter', '米', 'm' (excluding 'mm')
        - yard keywords: 'yard', '码', 'yd', exact 'y'

        Returns:
            (is_meter, is_yard) tuple of bools
        """
        uom_lower = uom_name.lower() if uom_name else ""
        is_meter = (
            any(kw in uom_lower for kw in ["meter", "米", "m"])
            and "mm" not in uom_lower
        )
        is_yard = (
            any(kw in uom_lower for kw in ["yard", "码", "yd"]) or uom_lower == "y"
        )
        return is_meter, is_yard

    def _check_order_soft_triggers(self, client, order_id: int) -> list[str]:
        """Check order for soft trigger conditions after adding a product.

        Checks for:
        - Mixed units (meters and yards in same order)
        - Same product with different units
        - All factory order products (shipping_days_min >= 3, no in-stock)

        Returns:
            List of triggered tags (e.g., ["mixed_units", "factory_order"])
        """
        triggers = []

        try:
            order_lines = client.get_order_lines(order_id)
            if not order_lines:
                return triggers

            # === Mixed Units Detection ===
            has_meter = False
            has_yard = False
            product_units: dict[int, set[str]] = {}

            for line in order_lines:
                product_uom = line.get("product_uom")
                uom_name = ""
                if isinstance(product_uom, list) and len(product_uom) > 1:
                    uom_name = product_uom[1]
                elif isinstance(product_uom, str):
                    uom_name = product_uom

                is_meter, is_yard = self._detect_uom(uom_name)
                if is_meter:
                    has_meter = True
                if is_yard:
                    has_yard = True

                pid = line.get("product_id")
                if isinstance(pid, list) and len(pid) > 0:
                    pid = pid[0]
                else:
                    pid = None

                if pid and (is_meter or is_yard):
                    product_units.setdefault(pid, set())
                    if is_meter:
                        product_units[pid].add("m")
                    if is_yard:
                        product_units[pid].add("y")

            if has_meter and has_yard:
                triggers.append("mixed_units")
                for units in product_units.values():
                    if "m" in units and "y" in units:
                        triggers.append("mixed_units_same_product")
                        break

            # === Factory Order Detection ===
            unique_pids = list(
                {
                    (pid[0] if isinstance(pid, list) and pid else None) or pid
                    for line in order_lines
                    if (pid := line.get("product_id"))
                }
            )
            unique_pids = [p for p in unique_pids if isinstance(p, int)]

            if unique_pids:
                products = client._call(
                    model="product.product",
                    method="read",
                    args=[unique_pids],
                    kwargs={"fields": ["id", "shipping_days_min"]},
                )
                if products:
                    has_factory = any(
                        (p.get("shipping_days_min") or 0) >= 3 for p in products
                    )
                    has_instock = any(
                        (p.get("shipping_days_min") or 0) == 1 for p in products
                    )
                    if has_factory and not has_instock:
                        triggers.append("factory_order")
        except Exception as e:
            logger.warning(f"[OdooTools] _check_order_soft_triggers error: {e}")

        return triggers

    def _extract_region_name(self, region_field) -> Optional[str]:
        """Extract region name from Odoo many2one field.

        Args:
            region_field: Odoo many2one field (can be list [id, name] or string)

        Returns:
            Region name as string, or None if not available
        """
        if not region_field:
            return None
        if isinstance(region_field, list) and len(region_field) > 1:
            return region_field[1]
        return region_field if isinstance(region_field, str) else None

    def _format_address_inline(self, address: dict) -> str:
        """Format address as a single line string.

        Args:
            address: Address dictionary with keys: street, street2, city, zip, state_id, country_id

        Returns:
            Formatted address string
        """
        parts = []

        if address.get("street"):
            parts.append(address["street"])
        if address.get("street2"):
            parts.append(address["street2"])

        city_parts = []
        if address.get("zip"):
            city_parts.append(str(address["zip"]))
        if address.get("city"):
            city_parts.append(address["city"])
        if city_parts:
            parts.append(" ".join(city_parts))

        if (
            address.get("state_id")
            and isinstance(address["state_id"], list)
            and len(address["state_id"]) > 1
        ):
            parts.append(address["state_id"][1])

        if (
            address.get("country_id")
            and isinstance(address["country_id"], list)
            and len(address["country_id"]) > 1
        ):
            parts.append(address["country_id"][1])

        return ", ".join(parts) if parts else "N/A"

    @staticmethod
    def _clean_contact_phone(address: dict) -> str:
        """Return the displayable phone for an address record, or "".

        Prefers `phone`, falls back to `mobile`. Placeholder junk Odoo data
        ("0", "false") counts as missing — showing "Tel: 0" helps nobody.
        """
        for key in ("phone", "mobile"):
            val = address.get(key)
            if val is None:
                continue
            val = str(val).strip()
            if val and val.lower() not in ("0", "false", "none", "-"):
                return val
        return ""

    def _compose_delivery_address_line(self, address: dict, lang: str = "en") -> str:
        """Delivery address prefixed with contact name + phone for the
        quotation card, e.g. "余師父(工場)（9387 0822），美麗都11字樓A1" —
        the customer can catch a wrong courier contact/phone before
        confirming instead of after the goods ship (2026-09-27 request).
        """
        name = (address.get("name") or "").strip()
        phone = self._clean_contact_phone(address)
        inline = self._format_address_inline(address)
        is_zh = lang.startswith("zh")

        if name and phone:
            head = f"{name}（{phone}）" if is_zh else f"{name} ({phone})"
        else:
            head = name or phone

        if not inline or inline == "N/A":
            return head or "N/A"
        if not head:
            return inline
        return f"{head}{'，' if is_zh else ', '}{inline}"

    @_require_binding
    def query_sample_book(self, book_number: str) -> str:
        """
        List in-stock products in a sample book (codes prefixed "6606-001"...).

        Use ONLY for explicit sample book/catalog requests with an exact 4-digit
        number — never for partial codes, OCR fragments, or as a fallback after
        a failed product lookup (use query_product with the full candidate).

        Args:
            book_number: 4-digit sample book number (e.g., "6606")

        Returns:
            Formatted product list with stock information
        """
        logger.info(f"[OdooTools] query_sample_book called with: {book_number}")

        # Validate book number format: must be exactly 4 digits
        digits = re.sub(r"[^0-9]", "", book_number)
        if len(digits) != 4:
            return (
                f"注意：樣本冊編號固定為 4 位數字。\n"
                f'您輸入的是 "{book_number}"（{len(digits)} 位數字），格式不正確。\n\n'
                f"正確格式示例：\n"
                f"- 樣本冊編號（4 位）：6606、2207\n"
                f"- 產品編號（6 位）：227707 或 AR227707"
            )

        book_number = digits

        try:
            # Unified client factory honors an injected session if one was set
            client = self._get_system_client()

            # Get products by sample book number (using default_code prefix)
            products = client.get_products_by_sample_book_number(
                book_number=book_number,
                only_in_stock=True,
                limit=100,
            )

            if not products:
                return f"Sample Book: {book_number}\nNo products in stock."

            # Format results
            lines = [f"Sample Book: {book_number}"]
            lines.append(f"Products in stock: {len(products)}")
            lines.append("")

            for p in products:
                code = p.get("default_code", "N/A")
                name = p.get("name", "N/A")
                price = p.get("secondary_uom_list_price") or p.get("list_price", 0)
                width = p.get("width_cm", 0)

                line = f"{code} - {name}"
                line += f" | In Stock"  # Stock status only, no quantity shown
                line += f" | Price: HK${price:.2f}/yd"

                if width:
                    line += f" | Width: {width:.0f}cm"

                lines.append(line)

            result_lines = ["📋 Sample book request noted for human follow-up"]
            result_lines.append("")
            result_lines.extend(lines)
            return "\n".join(result_lines)

        except Exception as e:
            logger.error(f"[OdooTools] query_sample_book error: {e}")
            return f"Error querying sample book: {str(e)}"

    def _trigger_sample_book_task(self, book_number: str) -> bool:
        """Deprecated compatibility shim.

        Sample book follow-up is handled by channel soft triggers now.
        """
        logger.info("Sample book runtime task creation is disabled")
        return False

    def _parse_product_info(self, output: str) -> Optional[Dict[str, any]]:
        """Parse product information from script output.

        Args:
            output: The stdout from query.py

        Returns:
            Dict with product_id, code, name, price, qty, region, weight, width, composition or None
        """
        if not output or "Error:" in output or "未找到產品" in output:
            return None

        # Parse product ID
        id_match = re.search(r"ID:\s+(\d+)", output)
        # Parse Product ID (for link) - this is the template ID used for generating online shop links
        product_link_id_match = re.search(r"Product ID \(for link\):\s+(\d+)", output)
        # Parse product code
        code_match = re.search(r"產品編碼:\s+(\S+)", output)
        # Parse product name (from the header line)
        name_match = re.search(r"產品:\s+(\S+)\s+-\s+(.+)", output)
        # Parse price
        price_match = re.search(r"標價:\s+HK\$(\d+\.?\d*)", output)
        # Parse quantity
        qty_match = re.search(r"實際庫存:\s+(\d+\.?\d*)", output)
        # Parse region of origin
        region_match = re.search(r"原產地:\s+(\S+)", output)
        # Parse average weight
        weight_match = re.search(r"平均重量:\s+(\d+\.?\d*)g", output)
        # Parse width
        width_match = re.search(r"寬度:\s+(\d+\.?\d*)cm", output)
        # Parse composition (description)
        composition_match = re.search(r"成分:\s+(.+)", output)

        if id_match and code_match:
            return {
                "product_id": int(id_match.group(1)),
                "product_tmpl_id": int(product_link_id_match.group(1))
                if product_link_id_match
                else None,
                "code": code_match.group(1),
                "name": name_match.group(2) if name_match else code_match.group(1),
                "price": float(price_match.group(1)) if price_match else None,
                "qty": float(qty_match.group(1)) if qty_match else None,
                "region_of_origin": region_match.group(1) if region_match else None,
                "average_weight_g": float(weight_match.group(1))
                if weight_match
                else None,
                "width_cm": float(width_match.group(1)) if width_match else None,
                "composition": composition_match.group(1).strip()
                if composition_match
                else None,
            }
        return None

    def _parse_customer_info(self, output: str) -> Optional[Dict[str, any]]:
        """Parse customer information from script output.

        Args:
            output: The stdout from order.py

        Returns:
            Dict with customer_id, name, remarks, default_shipping or None
        """
        if not output or "Error:" in output:
            return None

        # Look for customer ID pattern (typically "ID: xxxxx")
        id_match = re.search(r"ID:\s+(\d+)", output)
        # Look for customer name pattern (could be in various formats)
        name_match = re.search(r"姓名:\s*(.+?)(?:\n|公司:)", output)
        if not name_match:
            # Try alternative pattern
            name_match = re.search(r"客戶[：:]\s*(.+?)(?:\n|ID:)", output)

        # Look for customer remarks
        remarks_match = re.search(r"Remarks:\s*(.+?)(?:\n|$)", output)

        # Look for default shipping address
        default_shipping = None
        shipping_match = re.search(
            r"Default Shipping[：:]\s*(.+?)(?:\n|Remarks:|$)", output, re.DOTALL
        )
        if shipping_match:
            shipping_str = shipping_match.group(1).strip()
            # Try to parse address ID from shipping info
            addr_id_match = re.search(r"ID:\s*(\d+)", shipping_str)
            addr_id = int(addr_id_match.group(1)) if addr_id_match else None

            # Get address details
            street_match = re.search(r"Street:\s*(.+?)(?:\n|$)", shipping_str)
            city_match = re.search(r"City:\s*(.+?)(?:\n|$)", shipping_str)

            if addr_id:
                default_shipping = {
                    "id": addr_id,
                    "name": shipping_str.split("\n")[0].strip()
                    if shipping_str
                    else None,
                    "street": street_match.group(1).strip() if street_match else None,
                    "city": city_match.group(1).strip() if city_match else None,
                }

        if id_match:
            return {
                "customer_id": int(id_match.group(1)),
                "name": name_match.group(1).strip()
                if name_match
                else f"Customer {id_match.group(1)}",
                "remarks": remarks_match.group(1).strip() if remarks_match else None,
                "default_shipping": default_shipping,
            }
        return None

    def _parse_order_info(self, output: str) -> Optional[Dict[str, any]]:
        """Parse order information from script output.

        Args:
            output: The stdout from order.py

        Returns:
            Dict with order_id, partner_id, order_name or None
        """
        if not output or "Error:" in output:
            return None

        # Parse order ID (internal numeric ID)
        order_id_match = re.search(r"訂單 ID[：:]\s*(\d+)", output)
        if not order_id_match:
            order_id_match = re.search(r"Order ID[：:]\s*(\d+)", output)
        if not order_id_match:
            order_id_match = re.search(r"訂單[（(](\d+)[）)]", output)

        # Parse partner/customer ID
        partner_id_match = re.search(r"客戶 ID[：:]\s*(\d+)", output)
        if not partner_id_match:
            partner_id_match = re.search(r"Partner ID[：:]\s*(\d+)", output)
        if not partner_id_match:
            partner_id_match = re.search(r"Customer ID[：:]\s*(\d+)", output)

        # Parse order name/Reference (SOxxxxx format - PRIMARY IDENTIFIER)
        order_name_match = re.search(r"訂單編號[：:]\s*(\S+)", output)
        if not order_name_match:
            order_name_match = re.search(r"訂單名稱[：:]\s*(.+?)(?:\n|$)", output)
        if not order_name_match:
            order_name_match = re.search(r"Order Reference[：:]\s*(\S+)", output)
        if not order_name_match:
            order_name_match = re.search(r"Order[：:]\s*(.+?)(?:\n|$)", output)

        # Parse portal URL
        portal_url_match = re.search(r"Portal URL[：:]\s*(\S+)", output)

        if order_id_match:
            order_id = int(order_id_match.group(1))
            partner_id = int(partner_id_match.group(1)) if partner_id_match else None
            # Use order name (SOxxxxx) as primary identifier
            order_name = (
                order_name_match.group(1).strip()
                if order_name_match
                else f"SO{order_id}"
            )

            return {
                "order_id": order_id,
                "partner_id": partner_id,
                "order_name": order_name,
                "portal_url": portal_url_match.group(1).strip()
                if portal_url_match
                else None,
            }
        return None

    @_require_binding
    def query_product(
        self, product_code: str, pattern_hint: str = None, customer_width_cm=None
    ) -> str:
        """
        Query product info by 6-digit code; tracks the product for pronoun resolution.

        Output includes stock_info JSON. Handle status as: available → process
        normally; low_stock → show warning only (no auto-transfer from pure
        product queries); sold_out → cannot be ordered (NO backorders) — show
        guidance_message and guide the customer to cancel or browse other
        products in the same sample book; dropped → show guidance_message,
        block ordering; restocking → also show restock_date_display. Use
        requires_confirmation/confirmation_message/guidance_message fields as
        given.

        Args:
            product_code: 6-digit product code — pass user input as-is (tool
                strips AR prefix, extracts first 6 digits, merges 4+2 splits)
            pattern_hint: optional pattern/design the user mentioned ("checks",
                "plain", "素色"); appends PATTERN MISMATCH WARNING on mismatch
            customer_width_cm: width in cm the customer stated (門幅, e.g.
                150); appends WIDTH MISMATCH WARNING on mismatch with the
                system width

        Returns:
            Product information with stock status JSON
        """
        logger.info(f"[OdooTools] query_product called with code: {product_code}")

        # Factory-number inputs (contains "." / "/" / letters, or 5 pure digits)
        # must NEVER reach the digit-stripping path below — "499.893/2" would
        # be silently queried as "499893" (an unrelated product). Resolve via
        # the factory-code matcher instead.
        original_input = product_code
        if classify_code_input(original_input) == "factory_code":
            return self._query_product_by_factory_code(original_input)

        # Extract first 6 digits from user input
        code = re.sub(r"^AR", "", product_code, flags=re.IGNORECASE)
        digits = re.sub(r"[^0-9]", "", code)
        query_code = digits[:6] if len(digits) >= 6 else None
        notice_message = None

        if query_code and original_input.strip() != query_code:
            # User input differs from the 6-digit code being queried
            # Inform user about the 6-digit product code format
            notice_message = (
                f'注意：產品編號是六位數字碼。您輸入的是 "{original_input}"，'
                f'我將按照前6位數字 "{query_code}" 進行查詢。'
            )
        elif not query_code:
            # Not enough digits - cannot query
            if len(digits) > 0:
                return (
                    f'注意：產品編號固定為 6 位數字。您輸入的是 "{original_input}"（{len(digits)} 位數字），格式不正確。\n\n'
                    f"正確格式示例：\n"
                    f"- 樣本冊編號（4 位）：6606、2207\n"
                    f"- 產品編號（6 位）：227707 或 AR227707"
                )
            else:
                return (
                    f'注意：您輸入的 "{original_input}" 中不包含數字。\n\n'
                    f"正確格式示例：\n"
                    f"- 樣本冊編號（4 位）：6606、2207\n"
                    f"- 產品編號（6 位）：227707 或 AR227707"
                )

        # Use the extracted 6-digit code for query
        product_code = query_code

        # Check cache (cache key does NOT include pattern_hint)
        cache_key = f"product:{product_code}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug(f"[Cache] Hit for {cache_key}")
            result = cached
            # Update _last_product_name from cached result so pattern
            # mismatch detection works on cache hits too (Chase N3)
            for line in result.splitlines():
                if line.startswith("  Product Name:"):
                    self._last_product_name = line.split(":", 1)[1].strip()
                    break
            width_match = re.search(r"Width:\s*(\d+\.?\d*)\s*cm", result)
            self._last_product_width_cm = (
                float(width_match.group(1)) if width_match else None
            )
        else:
            # System account architecture: always use script fallback
            result = self._query_product_via_script(product_code)

        # Pattern mismatch detection (independent of cache)
        if pattern_hint and self._last_product_name:
            mismatch = self._check_pattern_mismatch(
                pattern_hint, self._last_product_name
            )
            if mismatch:
                result += "\n\n" + mismatch

        # Width mismatch detection (independent of cache)
        if customer_width_cm is not None:
            width_warning = self._check_width_mismatch(
                customer_width_cm,
                {"width_cm": getattr(self, "_last_product_width_cm", None)},
            )
            if width_warning:
                result += "\n\n" + width_warning

        return result

    def _query_product_via_script(self, product_code: str) -> str:
        """Fallback method to query product using OdooClient directly.

        Args:
            product_code: Product code

        Returns:
            Product information string
        """
        try:
            from ar_chatbot.scripts.lib.odoo_client import OdooClient as OdooClientLib

            client = self._get_system_client()
            product = client.search_product_by_code(product_code)
            if not product:
                # Fallback: fuzzy search on default_code
                fuzzy = client.search_product_by_code_fuzzy(product_code, limit=5)
                if fuzzy:
                    candidates = [
                        "- {}: {}".format(
                            p.get("default_code", "N/A"), p.get("name", "N/A")
                        )
                        for p in fuzzy
                    ]
                    return (
                        f"未找到精确匹配 {product_code}，找到相似产品：\n"
                        + "\n".join(candidates)
                        + "\n请确认是否为其中之一？"
                    )
                return f"Error: Product {product_code} not found"

            # Calculate stock status
            stock_info = OdooClientLib.calculate_stock_status(product)

            # Track product in context if available
            if self.context_manager:
                self.context_manager.add_product(
                    code=product.get("default_code", product_code),
                    name=product.get("name", ""),
                    product_id=product.get("id"),
                    metadata={
                        "price": product.get("list_price"),
                        "qty": product.get("qty_available"),
                        "region_of_origin": self._extract_region_name(
                            product.get("region_of_origin_id")
                        ),
                        "average_weight_g": product.get("average_weight_g"),
                        "width_cm": product.get("width_cm"),
                        "composition": product.get("description"),
                        "brand": self._extract_region_name(product.get("brand_id")),
                        "collection": product.get("collection_name_full"),
                        "sample_bookcat": product.get("sample_bookcat"),
                        "equiv_super_lv": product.get("equiv_super_lv"),
                        "stock_info": stock_info,
                    },
                )

            # Save product name for pattern mismatch detection (Chase N2: prefer over regex)
            self._last_product_name = product.get("name", "")
            self._last_product_width_cm = product.get("width_cm")

            return self._format_product_with_stock(product, stock_info)
        except Exception as e:
            logger.error(f"[OdooTools] _query_product_via_script error: {e}")
            return f"Error: {str(e)}"

    def _format_product_with_stock(self, product: dict, stock_info: dict) -> str:
        """Format product information with stock status.

        Args:
            product: Product data from Odoo
            stock_info: Stock status information

        Returns:
            Formatted product string
        """
        lines = []
        lines.append("")
        lines.append("=" * 60)
        lines.append(f"Product: {product.get('default_code')} - {product.get('name')}")
        lines.append("=" * 60)
        lines.append(f"  ID:              {product.get('id')}")
        # Product Template ID for generating online shop link
        # For product.product queries, product_tmpl_id is many2one [id, name]
        # For product.template queries, use id directly
        product_tmpl_id_raw = product.get("product_tmpl_id")
        if product_tmpl_id_raw:
            # Extract ID from many2one format [id, name]
            if isinstance(product_tmpl_id_raw, list):
                product_tmpl_id = product_tmpl_id_raw[0]
            else:
                product_tmpl_id = product_tmpl_id_raw
        else:
            product_tmpl_id = product.get("id")
        if product_tmpl_id:
            lines.append(f"  Product ID (for link): {product_tmpl_id}")
        lines.append(f"  Product Code:    {product.get('default_code')}")
        lines.append(f"  Product Name:    {product.get('name')}")

        description_sale = product.get("description_sale")
        if description_sale:
            lines.append(f"  Sales Description: {description_sale}")

        # New fields
        region_of_origin = product.get("region_of_origin_id")
        region_name = self._extract_region_name(region_of_origin)
        if region_name:
            europe_tag = " (Europe)" if region_name in EUROPEAN_COUNTRIES else ""
            lines.append(f"  Region of Origin: {region_name}{europe_tag}")

        avg_weight = product.get("average_weight_g")
        if avg_weight is not None:
            # Convert to int if it's a whole number
            weight_display = (
                int(avg_weight) if avg_weight == int(avg_weight) else avg_weight
            )
            lines.append(f"  Weight:         {weight_display} g/m")

        width = product.get("width_cm")
        if width is not None:
            lines.append(f"  Width:            {width}cm")

        composition = product.get("description")
        if composition:
            lines.append(f"  Composition:      {composition}")

        # Yarn count/fineness
        yarn_id = product.get("product_yarn_id")
        yarn_name = self._extract_region_name(yarn_id)
        if yarn_name:
            lines.append(f"  Yarn Count/Fineness: {yarn_name}")

        # Brand (many2one field: [id, name])
        brand_id = product.get("brand_id")
        brand_name = self._extract_region_name(brand_id)
        if brand_name:
            lines.append(f"  Brand:            {brand_name}")

        # Collection (char field: direct string)
        collection = product.get("collection_name_full")
        if collection:
            lines.append(f"  Collection:       {collection}")

        # Sample book category
        sample_bookcat = product.get("sample_bookcat")
        if sample_bookcat:
            lines.append(f"  Sample Book:      {sample_bookcat}")

        # Yarn count (Equiv Super LV)
        equiv_super_lv = product.get("equiv_super_lv")
        if equiv_super_lv:
            lines.append(f"  Yarn Count:       {equiv_super_lv}")

        # Prices
        list_price = product.get("list_price", 0)
        secondary_price = product.get("secondary_uom_list_price", list_price)
        lines.append(f"  Price (per yard):      HK${secondary_price:.2f}/yard")
        lines.append(f"  Price (per meter):     HK${list_price:.2f}/meter")

        # Stock quantity is intentionally hidden - only status is shown below

        # Stock status
        lines.append("")
        lines.append("Stock Status:")
        lines.append(f"  Status:           {stock_info['status_display']}")

        if stock_info.get("shipping_display"):
            lines.append(f"  Shipping:         {stock_info['shipping_display']}")

        if stock_info["restock_status"]:
            lines.append(f"  Restock Status:   {stock_info['restock_status']}")

        if stock_info["restock_date_display"]:
            lines.append(f"  Restock Date:     {stock_info['restock_date_display']}")

        if stock_info["confirmation_message"]:
            lines.append(f"  Confirmation:     {stock_info['confirmation_message']}")

        if stock_info["guidance_message"]:
            lines.append(f"  Guidance:         {stock_info['guidance_message']}")

        # Image URLs
        primary_image = product.get("primary_image_url")
        other_images_raw = product.get("other_image_urls", "")

        # Parse other_images - it's a newline-separated string
        other_images = []
        if other_images_raw:
            other_images = [
                url.strip() for url in other_images_raw.split("\n") if url.strip()
            ]

        if primary_image:
            lines.append(f"  Primary Image:   {primary_image}-medium")

        if other_images:
            lines.append(f"  Other Images:    {len(other_images)} image(s)")
            for idx, img_url in enumerate(other_images[:3], 1):
                lines.append(f"    [{idx}] {img_url}-medium")

        # Add images metadata as JSON for platform-specific handling
        lines.append("")
        lines.append("IMAGES_JSON:")
        images_metadata = {
            "primary": f"{primary_image}-medium" if primary_image else None,
            "others": [
                f"{url}-medium" for url in other_images[:5]
            ],  # Max 5 other images
        }
        lines.append(json.dumps(images_metadata, ensure_ascii=False))

        # Width check for narrow fabric (silent for standard width >= 145cm)
        width = product.get("width_cm")
        if width is not None and width > 0 and width < 145:
            lines.append("")
            lines.append("⚠️ NARROW FABRIC NOTICE:")
            lines.append(
                f"This fabric is narrower than standard (width: {width}cm < 145cm)."
            )
            lines.append("Please confirm with customer:")
            lines.append("  - Is the required yardage/meterage sufficient?")
            lines.append("  - Are they aware this is a narrow width fabric?")
            stock_info["width_warning"] = {
                "is_narrow": True,
                "width_cm": width,
                "threshold_cm": 145,
                "message": f"Narrow fabric: {width}cm < 145cm",
            }
        elif width is not None:
            # Silent check - mark as standard width (no message displayed)
            stock_info["width_warning"] = {
                "is_narrow": False,
                "width_cm": width,
                "threshold_cm": 145,
            }

        # Add stock_info as JSON for programmatic access
        lines.append("")
        lines.append("")
        lines.append("STOCK_INFO_JSON:")
        lines.append(json.dumps(stock_info, ensure_ascii=False, indent=2))
        lines.append("=" * 60)

        return "\n".join(lines)

    @_require_binding
    def query_brand(self, brand_name: str) -> str:
        """
        Query brand or collection information and provide category link.

        When a customer asks about a specific brand or collection, this tool will:
        1. Search for the brand by name (fuzzy match)
        2. If not found as brand, search for collection/series
        3. If found, return the category link
        4. If not found at all, list some brands and provide the brand list link

        Args:
            brand_name: Brand or collection name to query (e.g., "Hesworth", "8239", "Fantasia")

        Returns:
            Category link or list of brands with link to brand collection page
        """
        import time

        start_time = time.time()
        logger.info(f"[OdooTools] query_brand called with: {brand_name}")

        # Base URL for category links — derived from ODOO_URL so test/demo
        # environments return demo links (was hardcoded to www before).
        base_url = normalize_odoo_base_url(
            os.environ.get("ODOO_URL") or "https://www.artextilestore.com"
        )
        CATEGORY_BASE_URL = f"{base_url}/shop/category"
        BRAND_LIST_URL = f"{CATEGORY_BASE_URL}/brand-collection-1"

        # Brand abbreviation mapping (common short names -> full names)
        BRAND_ABBREVIATIONS = {
            "VBC": "Vitale Barberis Canonico",
            "VBC Canonico": "Vitale Barberis Canonico",
            "Loro Piana": "Loro Piana",
            "LP": "Loro Piana",
            "Zegna": "Zegna",
            "Ermenegildo Zegna": "Zegna",
            "Cerruti": "Cerruti 1881",
            "Fratelli Piacenza": "Fratelli Piacenza",
            "Piacenza": "Fratelli Piacenza",
            "Tallia Di Delfino": "Fratelli Tallia Di Delfino",
            "TDD": "Fratelli Tallia Di Delfino",
        }

        # Check if the input is a known abbreviation
        search_term = brand_name.strip()
        if search_term.upper() in BRAND_ABBREVIATIONS:
            search_term = BRAND_ABBREVIATIONS[search_term.upper()]
            logger.info(
                f"[OdooTools] query_brand: Expanded abbreviation '{brand_name}' to '{search_term}'"
            )

        # System account architecture: always use system client
        client = self._get_system_client()

        try:
            # Step 1: Search for brand (direct children of Brand & Collection)
            t0 = time.time()
            matching_brands = client.search_brand_category_by_name(
                search_term, exact=False
            )
            logger.info(
                f"[OdooTools] query_brand: search_brand_category_by_name took {time.time() - t0:.2f}s, found {len(matching_brands) if matching_brands else 0} brands"
            )

            if matching_brands:
                # Found matching brand(s)
                lines = []
                lines.append("")
                lines.append("=" * 60)
                lines.append(f"Brand Query: '{brand_name}'")
                lines.append("=" * 60)

                for brand in matching_brands:
                    brand_id = brand.get("id")
                    brand_name_found = brand.get("name")
                    brand_link = f"{CATEGORY_BASE_URL}/{brand_id}"

                    lines.append(f"\n✓ Brand: {brand_name_found}")
                    lines.append(f"  Link: {brand_link}")

                logger.info(
                    f"[OdooTools] query_brand: Total time {time.time() - start_time:.2f}s"
                )
                return "\n".join(lines)

            # Step 2: Brand not found, search for collection/series (all descendants)
            t0 = time.time()
            matching_collections = client.search_collection_by_name(
                search_term, exact=False
            )
            logger.info(
                f"[OdooTools] query_brand: search_collection_by_name took {time.time() - t0:.2f}s, found {len(matching_collections) if matching_collections else 0} collections"
            )

            if matching_collections:
                # Found matching collection(s)
                lines = []
                lines.append("")
                lines.append("=" * 60)
                lines.append(f"Collection Query: '{brand_name}'")
                lines.append("=" * 60)

                for collection in matching_collections:
                    collection_id = collection.get("id")
                    collection_name = collection.get("name")
                    collection_link = f"{CATEGORY_BASE_URL}/{collection_id}"

                    lines.append(f"\n✓ Collection: {collection_name}")
                    lines.append(f"  Link: {collection_link}")

                logger.info(
                    f"[OdooTools] query_brand: Total time {time.time() - start_time:.2f}s"
                )
                return "\n".join(lines)

            # Step 3: Not found at all - list some brands and provide link
            t0 = time.time()
            all_brands = client.get_all_brand_categories(limit=50)
            logger.info(
                f"[OdooTools] query_brand: get_all_brand_categories took {time.time() - t0:.2f}s"
            )

            lines = []
            lines.append("")
            lines.append("=" * 60)
            lines.append(f"Brand/Collection Query: '{brand_name}'")
            lines.append("=" * 60)
            lines.append(
                f"\nSorry, we couldn't find '{brand_name}' in our brands or collections."
            )
            lines.append("\nSome brands we carry:")

            if all_brands:
                for brand in all_brands[:10]:  # Show first 10 brands
                    brand_name_found = brand.get("name")
                    lines.append(f"  - {brand_name_found}")
                if len(all_brands) > 10:
                    lines.append(f"  ... and {len(all_brands) - 10} more brands")
            else:
                lines.append("  No brands found in the system.")

            lines.append(f"\n📋 View all brands: {BRAND_LIST_URL}")

            logger.info(
                f"[OdooTools] query_brand: Total time {time.time() - start_time:.2f}s"
            )
            return "\n".join(lines)

        except Exception as e:
            logger.error(f"[OdooTools] query_brand error: {e}")
            return f"Error querying brand: {str(e)}"

    def query_collections(self, name: str, exact: bool = False, limit: int = 10) -> str:
        """Search collections/series by name (fuzzy by default).

        Same definition as the oa_chat customer sales report: every
        product.public.category under the "Brand & Collection" root (id=1,
        excluding the root) — brands, collections/series and bunches (sample
        books) all included. Each hit shows its parent category (breadcrumb)
        and the shop category link.

        Args:
            name: Collection/series name (e.g. "8239", "Fantasia", "Hesworth")
            exact: Exact match instead of case-insensitive partial match
            limit: Maximum number of results

        Returns:
            Formatted collection list string
        """
        import time

        start_time = time.time()
        logger.info(f"[OdooTools] query_collections called with: {name!r} (exact={exact})")

        base_url = normalize_odoo_base_url(
            os.environ.get("ODOO_URL") or "https://www.artextilestore.com"
        )
        CATEGORY_BASE_URL = f"{base_url}/shop/category"

        client = self._get_system_client()
        try:
            matches = client.search_collection_by_name(name, exact=exact, limit=limit)
            lines = [
                "",
                "=" * 60,
                f"Collections Query: '{name}'" + (" (exact)" if exact else ""),
                "=" * 60,
                "Scope: all categories under Brand & Collection "
                "(brands, collections/series, bunches)",
            ]
            if not matches:
                lines.append(
                    f"\nNo collection matches '{name}'. "
                    "Try a shorter keyword or check spelling."
                )
                return "\n".join(lines)

            lines.append(f"\nFound {len(matches)} match(es):")
            for cat in matches:
                cat_id = cat.get("id")
                cat_name = cat.get("name")
                parent = cat.get("parent_id") or [None, ""]
                parent_name = parent[1] if isinstance(parent, list) else str(parent)
                lines.append(f"\n✓ {cat_name}")
                if parent_name:
                    lines.append(f"  Parent: {parent_name}")
                lines.append(f"  Link: {CATEGORY_BASE_URL}/{cat_id}")

            logger.info(
                f"[OdooTools] query_collections: {len(matches)} hits in "
                f"{time.time() - start_time:.2f}s"
            )
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"[OdooTools] query_collections error: {e}")
            return f"Error querying collections: {str(e)}"

    @_require_binding
    def search_products_by_name(self, name: str, limit: int = 10) -> str:
        """
        Search products by name; tracks the first result in conversation context.

        Each result carries stock_info JSON — handle status as: available →
        process normally; low_stock → warning only (no auto-transfer); sold_out
        → cannot be ordered (NO backorders) — show guidance_message and guide
        the customer to cancel or browse other products in the same sample
        book; dropped → block ordering; restocking → also show
        restock_date_display.

        Args:
            name: Product name keyword
            limit: Maximum number of results

        Returns:
            Search results string with stock status for each product
        """
        # System account architecture: always use script fallback
        return self._search_products_by_name_via_script(name, limit)

    def _search_products_by_name_via_script(self, name: str, limit: int) -> str:
        """Fallback method to search products by name using OdooClient directly.

        Args:
            name: Product name keyword
            limit: Maximum number of results

        Returns:
            Search results string
        """
        try:
            from ar_chatbot.scripts.lib.odoo_client import OdooClient as OdooClientLib

            client = self._get_system_client()
            products = client.search_products_by_name(name, limit)
            if not products:
                return f"No products found matching '{name}'"

            lines = []
            lines.append(f"\nFound {len(products)} product(s):\n")

            for i, product in enumerate(products, 1):
                # Calculate stock status
                stock_info = OdooClientLib.calculate_stock_status(product)

                # Track first product in context if available
                if i == 1 and self.context_manager:
                    self.context_manager.add_product(
                        code=product.get("default_code", ""),
                        name=product.get("name", ""),
                        product_id=product.get("id"),
                        metadata={
                            "price": product.get("list_price"),
                            "qty": product.get("qty_available"),
                            "region_of_origin": self._extract_region_name(
                                product.get("region_of_origin_id")
                            ),
                            "average_weight_g": product.get("average_weight_g"),
                            "width_cm": product.get("width_cm"),
                            "composition": product.get("description"),
                            "brand": self._extract_region_name(product.get("brand_id")),
                            "collection": product.get("collection_name_full"),
                            "sample_bookcat": product.get("sample_bookcat"),
                            "equiv_super_lv": product.get("equiv_super_lv"),
                            "stock_info": stock_info,
                        },
                    )

                # Format each product
                lines.append(
                    f"[{i}] " + self._format_product_with_stock(product, stock_info)
                )

            return "\n".join(lines)
        except Exception as e:
            logger.error(f"[OdooTools] _search_products_by_name_via_script error: {e}")
            return f"Error: Search failed - {str(e)}"

    @_require_binding
    def check_stock(self, limit: int = 20) -> str:
        """
        Check products with available stock.
        Does not track context (use query_product for tracking).

        Args:
            limit: Maximum number of results

        Returns:
            Products in stock string
        """
        # Check cache
        cache_key = f"stock:{limit}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug(f"[Cache] Hit for {cache_key}")
            return cached

        try:
            from ar_chatbot.scripts.lib.odoo_client import OdooClient as OdooClientLib

            client = self._get_system_client()
            products = client.search_products_with_stock(limit)

            if not products:
                result = "\nNo products found with stock"
                self._cache.set(cache_key, result, self._CACHE_TTL_STOCK)
                return result

            lines = [f"\nProducts with stock ({len(products)} items):\n"]
            for product in products:
                lines.append(
                    self._format_product_with_stock(
                        product, OdooClientLib.calculate_stock_status(product)
                    )
                )
            result = "\n".join(lines)
            self._cache.set(cache_key, result, self._CACHE_TTL_STOCK)
            return result
        except Exception as e:
            logger.error(f"[OdooTools] check_stock error: {e}")
            return f"Error: {str(e)}"

    @_require_binding
    def search_customer(self, name: str) -> str:
        """
        Search customer by name.
        Tracks the customer in conversation context.

        IMPORTANT: Customer remarks (x_studio_customer_remarks) are included
        in the results. These may contain important constraints for:
        - Logistics/shipping requirements
        - Discount thresholds
        - Brand restrictions
        - Other limitations

        Always review the remarks before proceeding with orders!

        SECURITY NOTE FOR PORTAL USERS:
        Portal users can ONLY view their own customer information.
        Searching for other customers is NOT allowed.

        Args:
            name: Customer name keyword

        Returns:
            Customer search results with remarks (if any)
        """
        # Portal user restriction: can only view own customer info
        if self.is_portal_user():
            binding = self.get_binding()
            if binding:
                return (
                    f"出於隱私保護，您只能查看自己的客戶資訊。\n\n"
                    f"您的客戶資訊：\n"
                    f"- 客戶名稱：{binding.company_name or binding.display_name}\n\n"
                    f"如需查看其他客戶資訊，請聯繫銷售代表。"
                )

        # Check cache (only for non-portal path)
        user_type = "internal"
        if self.binding_service and self.platform_user_id:
            b = self.binding_service.db.get_binding(
                self.platform_type, self.platform_user_id
            )
            if b:
                user_type = b.user_type
        cache_key = f"customer:{name}:{user_type}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug(f"[Cache] Hit for {cache_key}")
            return cached

        try:
            client = self._get_system_client()
            customers = client.search_customer(name, limit=10)

            if not customers:
                result = f"\nNo customers found containing '{name}'"
                self._cache.set(cache_key, result, self._CACHE_TTL_CUSTOMER)
                return result

            lines = [f"\nFound {len(customers)} customer(s):"]
            lines.append("-" * 60)

            for i, c in enumerate(customers, 1):
                lines.append(f"  [{i}] ID: {c.get('id')} | {c.get('name')}")

                # Customer remarks are NOT displayed to user, but stored in metadata
                remarks = c.get("x_studio_customer_remarks")

                # Get default shipping address
                addresses = client.get_customer_addresses(c.get("id"))
                default_shipping = None
                for addr in addresses:
                    if addr.get("type") == "delivery":
                        default_shipping = {
                            "id": addr.get("id"),
                            "name": addr.get("name"),
                            "street": addr.get("street"),
                            "street2": addr.get("street2"),
                            "city": addr.get("city"),
                        }
                        lines.append(f"      Default Shipping: {addr.get('name')}")
                        lines.append(f"          ID: {addr.get('id')}")
                        if addr.get("street"):
                            lines.append(f"          Street: {addr.get('street')}")
                        if addr.get("city"):
                            lines.append(f"          City: {addr.get('city')}")
                        break

                # Track customer in context if available
                if self.context_manager and i == 1:
                    self.context_manager.add_customer(
                        name=c.get("name"),
                        customer_id=c.get("id"),
                        metadata={
                            "email": c.get("email"),
                            "phone": c.get("phone"),
                            "remarks": remarks,
                            "default_shipping": default_shipping,
                        },
                    )

            lines.append("-" * 60)
            output = "\n".join(lines)
            self._cache.set(cache_key, output, self._CACHE_TTL_CUSTOMER)
            return output
        except Exception as e:
            logger.error(f"[OdooTools] search_customer error: {e}")
            return f"Error: {str(e)}"

    def get_product_link(self, default_code: str, product_id: int) -> str:
        """
        Get product online shop link.

        Use this ONLY when the user explicitly asks for:
        - Product link / URL / 網址 / 連結 / 線上連結 / link
        - Product webpage / 詳情頁
        - Product images / photos (share link instead of image output)

        Do NOT append links to ordinary product replies. Relay the returned
        URL verbatim and complete — never abbreviate or truncate it.

        Args:
            default_code: Product code (6 digits, e.g., "822917")
            product_id: Product template ID (e.g., 70800)

        Returns:
            Product online shop URL: {base_url}/shop/{default_code}-{product_id}
        """
        # System account architecture: always use system client
        client = self._get_system_client()

        try:
            url = client.get_product_online_url(default_code, product_id)
            return url
        except Exception as e:
            return f"Error generating product link: {str(e)}"

    def create_order(
        self, customer: str, shipping_id: str = None, invoice_id: str = None
    ) -> str:
        """
        Create a new sales order (tracks it in conversation context).

        ⚠️ Call get_price_quote() FIRST and only create after the customer
        confirms the price. Permissions: portal users order for their own
        account; internal users must specify a customer; unbound users get a
        binding prompt in the error ("我要綁定" → binding link).

        Args:
            customer: Customer ID or name keyword
            shipping_id: Delivery "Address ID" from list_customer_addresses
                (e.g. "17282") — the actual ID, NOT the [1], [2] sequence number
            invoice_id: Invoice Address ID (same format as shipping_id)

        Returns:
            Order creation result (incl. Portal URL) or permission error
        """
        # Permission validation for logged-in users
        if self.binding_service:
            binding = self.get_binding()

            # Parse customer_id (may be numeric ID or name keyword)
            customer_id = None
            if customer and customer.isdigit():
                customer_id = int(customer)

            # Check permission
            from ar_chatbot.auth.permission_checker import OrderPermissionChecker

            permission = OrderPermissionChecker.check_create_order_permission(
                binding, customer_id
            )

            if not permission.allowed:
                return f"Permission Denied: {permission.reason}"

        try:
            client = self._get_system_client()

            # Resolve customer. Portal users: unresolvable strings fall
            # back to their own account, foreign partners are denied —
            # name-based requests bypassed the numeric permission check
            # above (see _resolve_partner_for_binding).
            partner_id, resolve_error = self._resolve_partner_for_binding(customer)
            if resolve_error or not partner_id:
                return resolve_error

            # 按单下单主体硬闸(邮箱↔公司多对多,如 navi 买手 linda):
            # Gmail 代理模式下,目标客户 ≠ 绑定默认公司 且 本线程没有
            # 客服确认过的主体时,拒绝建单 — prompt 规则会被 LLM 绕过,
            # 权限层是最后防线。确认后由 confirm_account 步骤写入标记放行。
            if (
                getattr(self, "platform_type", "") == "gmail"
                and self.binding_service
                and self.context_manager
            ):
                from ar_chatbot.auth.permission_checker import (
                    OrderPermissionChecker,
                )

                binding = self.get_binding()
                if binding and getattr(binding, "is_valid", False):
                    perm = OrderPermissionChecker.check_create_order_permission(
                        binding, partner_id
                    )
                    default_pid = getattr(binding, "partner_id", None)
                    if (
                        getattr(binding, "user_type", "") == "internal"
                        and perm.allowed
                        and partner_id != default_pid
                    ):
                        confirmed = self.context_manager.get_confirmed_acting_customer()
                        label = (confirmed or {}).get("label") or ""
                        cust_str = str(customer or "")
                        ok = confirmed and (
                            confirmed.get("partner_id") == partner_id
                            or (
                                label
                                and cust_str
                                and (label in cust_str or cust_str in label)
                            )
                        )
                        if not ok:
                            return (
                                "⛔ ACTING CUSTOMER UNCONFIRMED — 建单被拒。"
                                f"目标客户解析为 #{partner_id}"
                                f"（默认绑定公司为 #{default_pid}），"
                                "邮箱↔公司是多对多,须客服先确认本单主体。"
                                "请勿重试建单;在回复文本中独立输出一行"
                                f"`ACTING_CUSTOMER: {customer or partner_id}`"
                                "并停止,等待客服在 Chatwoot 确认。"
                            )

            # Parse address IDs
            shipping_id_int = int(shipping_id) if shipping_id else None
            invoice_id_int = int(invoice_id) if invoice_id else None

            # Safety net: auto-fill shipping from last order if not provided
            if not shipping_id_int:
                last_sid = self._get_last_order_shipping_id(partner_id)
                if last_sid:
                    shipping_id_int = last_sid

            # Resolve target company: pin the sale.order to a specific Odoo
            # company (ODOO_COMPANY_ID) so orders don't silently follow the
            # system user's current company. e.g. pin Artextile (id=1) so WeChat
            # orders never land on 承达 even if the account's default changes.
            env = get_odoo_env(self)
            company_id_raw = env.get("ODOO_COMPANY_ID")
            company_id = int(company_id_raw) if company_id_raw else None

            # Create order
            order_id = client.create_order(
                partner_id,
                partner_shipping_id=shipping_id_int,
                partner_invoice_id=invoice_id_int,
                company_id=company_id,
            )

            # Get order name
            order = client.get_order(order_id)
            order_name = (
                order.get("name", f"SO{order_id}") if order else f"SO{order_id}"
            )

            # Build output. The portal link is emitted as a marker: the channel
            # renders the real URL at send time. Handing the raw URL to the LLM
            # got it rewritten to a fabricated domain with the access_token
            # dropped (incident 2026-08-19).
            lines = []
            lines.append("\nOrder created successfully!")
            lines.append(f"  Order Reference: {order_name}")
            lines.append(f"  Customer ID: {partner_id}")
            lines.append(f"  [ORDER_LINK:{order_name}]")

            output = "\n".join(lines)

            # Track order in context if available
            if self.context_manager:
                self.context_manager.add_order(
                    order_id=order_id,
                    partner_id=partner_id,
                    order_name=order_name,
                    metadata={"state": "draft"},
                )

            # Invalidate caches affected by order creation
            self._cache.invalidate("customer:")
            self._cache.invalidate("stock:")

            return output
        except Exception as e:
            logger.error(f"[OdooTools] create_order error: {e}")
            return f"Error: {str(e)}"

    @_require_binding
    def get_price_quote(self, customer: str, product_spec: str) -> str:
        """
        Customer-specific price quote — MUST be called (and shown to the
        customer for confirmation) BEFORE create_order(). Meter specs use the
        meter price, yard specs the yard price. Portal users see only their
        final price (no original price/discount %).

        Args:
            customer: Customer ID or name
            product_spec: e.g. "227707:3m", "227707:2y"

        Returns:
            PRICE BREAKDOWN (list price, customer price, discount, subtotal)
        """
        try:
            from ar_chatbot.scripts.lib.odoo_client import OdooClient as OdooClientLib
            from ar_chatbot.scripts.order import parse_product_spec

            client = self._get_system_client()

            # Resolve customer. Portal users are pinned to their own
            # account (unresolvable strings fall back to it, foreign
            # partners are rejected) — see _resolve_partner_for_binding.
            partner_id, resolve_error = self._resolve_partner_for_binding(customer)
            if resolve_error or not partner_id:
                return resolve_error

            # Parse product spec
            try:
                spec = parse_product_spec(product_spec)
            except ValueError as e:
                return f"\n[ERROR] Invalid product spec '{product_spec}': {e}\nExpected format: code:qty (e.g., 227707:1m, 227707:3y)"

            # Factory-number spec codes ("499.893/2:2m") must resolve via the
            # factory-code matcher — digit-stripping would quote the wrong
            # product ("499893").
            factory_notice = None
            raw_code = spec.get("raw_code") or spec["code"]
            if classify_code_input(raw_code) == "factory_code":
                ar_code, factory_notice, factory_error = (
                    self._resolve_factory_spec_code(raw_code)
                )
                if factory_error:
                    return f"\n{factory_error}"
                spec["code"] = ar_code

            # Query product
            product = client.search_product_by_code(spec["code"])
            if not product:
                return f"\nProduct not found: {spec['code']}"

            product_id = product["id"]
            unit = spec.get("unit", "y")  # 'm' for meters, 'y' for yards
            qty = spec["qty"]

            # ========== Minimum Order Quantity Check (per-brand) ==========
            moq_val, moq_unit, MIN_QTY_M, MIN_QTY_YD = self._get_moq_for_product(
                product
            )
            moq_label = f"{moq_val}{moq_unit}"

            if unit == "m" and qty < MIN_QTY_M:
                return (
                    f"\n[ERROR] 數量低於最小採購量。"
                    f"您要求的數量為 {qty}m（約 {qty / 0.9144:.4f}yd），"
                    f"最小採購量為 {moq_label}（≈ {MIN_QTY_YD:.4f}yd / {MIN_QTY_M:.4f}m）。"
                    f"\nMinimum order quantity is {moq_label} (≈ {MIN_QTY_YD:.4f}yd / {MIN_QTY_M:.4f}m). "
                    f"Your requested quantity ({qty}m) is below the minimum."
                )
            elif unit == "y" and qty < MIN_QTY_YD:
                return (
                    f"\n[ERROR] 數量低於最小採購量。"
                    f"您要求的數量為 {qty}yd（約 {qty * 0.9144:.4f}m），"
                    f"最小採購量為 {moq_label}（≈ {MIN_QTY_YD:.4f}yd / {MIN_QTY_M:.4f}m）。"
                    f"\nMinimum order quantity is {moq_label} (≈ {MIN_QTY_YD:.4f}yd / {MIN_QTY_M:.4f}m). "
                    f"Your requested quantity ({qty}yd) is below the minimum."
                )

            # Select price and UOM based on unit
            uom_id = None
            if unit == "m":
                list_price = product.get("list_price", 0)
                price_label = "/m"
                uom_id = client.get_uom_by_name("M")
                uom_id = uom_id["id"] if uom_id else None
                display_meters = qty
                display_yards = qty / 0.9144
            else:
                list_price = product.get(
                    "secondary_uom_list_price", product.get("list_price", 0)
                )
                price_label = "/yd"
                uom_id = client.get_uom_by_name("Y")
                uom_id = uom_id["id"] if uom_id else None
                display_yards = qty
                display_meters = qty * 0.9144

            stock_info = OdooClientLib.calculate_stock_status(product)
            if not stock_info["can_order"]:
                return self._unorderable_stock_error(stock_info)

            cache_key = f"quote:{customer}:{product_spec}"
            runtime_triggers = self._collect_order_soft_triggers(
                stock_info, display_meters
            )
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.debug(f"[Cache] Hit for {cache_key}")
                return self._append_soft_triggers(cached, runtime_triggers)

            # Use OdooClient.get_price_quote for pricelist logic
            quote = client.get_price_quote(
                partner_id=partner_id,
                product_id=product_id,
                qty=qty,
                uom_id=uom_id,
                unit_price=list_price,
            )

            if not quote:
                return f"\nFailed to get price quote for product {spec['code']}"

            price_unit = quote["price_unit"]
            actual_discount = quote["discount"]
            subtotal = quote["subtotal"]
            actual_price = price_unit * (1 - actual_discount / 100)

            # Also show the other price for reference
            if unit == "m":
                yard_price = product.get("secondary_uom_list_price")
                other_price_label = (
                    f"Yard Price: HK$ {yard_price:.2f}/yd" if yard_price else ""
                )
            else:
                meter_price = product.get("list_price")
                other_price_label = (
                    f"Meter Price: HK$ {meter_price:.2f}/m" if meter_price else ""
                )

            # Build output
            lines = []
            lines.append("\n" + "=" * 60)
            lines.append("Customer Price Quote")
            lines.append("=" * 60)
            lines.append(f"\nCustomer ID: {partner_id}")
            lines.append(f"Product: {spec['code']} - {product.get('name')}")
            # Order unit FIRST, other unit in parentheses. Yards-first display
            # used to make the model relabel meter orders as "yd" in its
            # summaries (prod 2026-08-17: "10yd" for a 10m order).
            if unit == "m":
                lines.append(f"Quantity: {display_meters:.2f}m ({display_yards:.4f}yd)")
            else:
                lines.append(f"Quantity: {display_yards:.4f}yd ({display_meters:.2f}m)")
            lines.append(f"Order Unit: {'Meters' if unit == 'm' else 'Yards'}")
            lines.append("-" * 60)

            has_discount = actual_discount > 0

            if has_discount:
                lines.append("PRICE BREAKDOWN (with discount applied):")
                lines.append(f"   List Price:      HK$ {list_price:.2f}{price_label}")
                lines.append(f"   Your Price:      HK$ {actual_price:.2f}{price_label}")
                lines.append(f"   Your Discount: {actual_discount}% OFF")
                lines.append(
                    f"   -> You save:      HK$ {list_price * qty - subtotal:.2f}"
                )
            else:
                lines.append("PRICE BREAKDOWN:")
                lines.append(f"   List Price:      HK$ {list_price:.2f}{price_label}")
                lines.append(
                    f"   Your Price:      HK$ {actual_price:.2f}{price_label} (no discount)"
                )

            if other_price_label:
                lines.append(f"   ({other_price_label} for reference)")

            lines.append("-" * 60)
            lines.append(f"Subtotal: HK$ {subtotal:.2f}")
            if has_discount:
                lines.append(f"   (includes {actual_discount}% discount)")
            # Lead-time / restock info so the agent can flag 現貨 vs 訂料 and
            # restocking dates at QUOTE time (before address/carrier steps).
            if stock_info.get("shipping_display"):
                lines.append(f"Shipping: {stock_info['shipping_display']}")
            if stock_info.get("restock_status") == "restocking" and stock_info.get(
                "restock_date_display"
            ):
                lines.append(f"Restock: {stock_info['restock_date_display']}")
            lines.append("=" * 60)
            if factory_notice:
                lines.append(factory_notice)

            output = "\n".join(lines)

            # Portal user: hide original price and discount info
            if self.is_portal_user():
                output = self._sanitize_price_output_for_portal(output)

            self._cache.set(cache_key, output, self._CACHE_TTL_QUOTE)

            return self._append_soft_triggers(output, runtime_triggers)
        except Exception as e:
            logger.error(f"[OdooTools] get_price_quote error: {e}")
            return f"Error: {str(e)}"

    def _sanitize_price_output_for_portal(self, output: str) -> str:
        """Remove original price and discount info from price quote output.

        Portal users should only see the final price they will pay,
        not the original price or discount percentage.

        Args:
            output: Original price quote output

        Returns:
            Sanitized output without original price/discount
        """
        import re

        # Remove lines containing "List Price" or "原價" or "Discount" or "折扣"
        lines = output.split("\n")
        sanitized_lines = []

        skip_patterns = [
            r"[原價定價].*:",
            r"[Ll]ist\s*[Pp]rice",
            r"[Dd]iscount",
            r"折扣",
            r"HK\$.*\s*->\s*",  # Price comparison lines like "HK$ 100 -> HK$ 80"
        ]

        for line in lines:
            skip = False
            for pattern in skip_patterns:
                if re.search(pattern, line):
                    skip = True
                    break
            if not skip:
                sanitized_lines.append(line)

        # Add note about pricing
        sanitized = "\n".join(sanitized_lines)
        if sanitized:
            sanitized += "\n\n(系統已為您應用最優價格)"

        return sanitized

    # Label-remark heuristic (see rules/so-line-customer-po-vs-remarks.md).
    # A label instruction is a token that ENDS in 标/標 — including count
    # forms (一個標, 2块标) and orientation combos (正标/反标) — or a
    # standalone orientation token 正/反. Bare 正/反 INSIDE a longer token
    # is NOT an instruction: that substring rule misrouted person names
    # （BJ赵正宇）, colors 正紅, and 反光 into remarks.
    _LABEL_TAIL_RE = re.compile(
        r"^(.+?)("
        r"[0-9０-９一二三四五六七八九十百千半两][塊块個个張张枚]?[标標]"
        r"|[正反][标標]"
        r")$"
    )
    _TRAILING_PUNCT = "。．.;；,，、!！?？)）]」』\"'"

    @classmethod
    def _classify_line_text_token(cls, token: str) -> tuple[str, str]:
        """Classify one customer-text token → (customer_po_fragment,
        label_remark_fragment); exactly one is empty unless the token
        embeds both, e.g. "PO号一個標" → ("PO号", "一個標")."""
        core = token.rstrip(cls._TRAILING_PUNCT)
        if not core:
            return token, ""
        m = cls._LABEL_TAIL_RE.match(core)
        if m:
            # Leading text + trailing label instruction written without
            # a space: keep the leading text as Customer PO material.
            return m.group(1), m.group(2)
        if core.endswith(("标", "標")):
            return "", core
        if core in ("正", "反"):
            return "", core
        return token, ""

    @classmethod
    def _split_label_remark_text(cls, *parts: Optional[str]) -> tuple[str, str]:
        """Split customer line text into (customer_po_text, label_remark_text).

        Label instructions (see _classify_line_text_token) go to the line's
        `remarks` field; every other token — PO numbers, style notes like
        "做馬夾" or "（BJ赵正宇）" — is Customer PO material.
        """
        po_tokens = []
        remark_tokens = []
        for part in parts:
            for token in str(part or "").split():
                po_frag, remark_frag = cls._classify_line_text_token(token)
                if po_frag:
                    po_tokens.append(po_frag)
                if remark_frag:
                    remark_tokens.append(remark_frag)
        return " ".join(po_tokens), " ".join(remark_tokens)

    # Customer-vs-internal remark convention (2026-09-28): the line `remarks`
    # field also carries sales/production entries (style notes, 正；/反；
    # orientation prefixes, contacts) that must never reach the customer.
    # Bot-written customer text is stored wrapped in 「」; customer-facing
    # renders extract ONLY the wrapped segments — anything unmarked (sales
    # entries, pre-convention bot text) is treated as internal.
    _CUSTOMER_REMARK_RE = re.compile(r"「([^」]*)」")

    @classmethod
    def _mark_customer_remark(cls, text: str) -> str:
        """Wrap bot-written customer label text for storage (「一個標」)."""
        return f"「{text}」"

    @classmethod
    def _extract_customer_remarks(cls, raw: Optional[str]) -> str:
        """Customer-visible portion of a line's remarks: the 「」-wrapped
        segments only (brackets stripped), space-joined. Empty when the
        remarks carry no customer text (internal-only → nothing to echo)."""
        if not raw:
            return ""
        return " ".join(
            seg.strip() for seg in cls._CUSTOMER_REMARK_RE.findall(raw) if seg.strip()
        )

    @classmethod
    def _route_line_customer_po(
        cls, client, customer_po: Optional[str], line_remark: Optional[str]
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Route customer PO + free-text remark by the label-remark heuristic.

        Label-instruction tokens (ending in 标/標, or standalone 正/反 —
        see `_classify_line_text_token`) go to the line's `remarks` field;
        the rest (PO number + ordinary remark text) is joined into
        `customer_po`, truncated to the field size.

        Returns (po_value, remarks_value, truncation_warning); values are
        None when empty.
        """
        po_text, remark_text = cls._split_label_remark_text(customer_po, line_remark)
        value = po_text or None

        warning = None
        if value:
            try:
                field_meta = client.get_field_type("sale.order.line", "customer_po")
                max_size = int((field_meta or {}).get("size") or 0)
            except (TypeError, ValueError):
                max_size = 0
            if max_size and len(value) > max_size:
                value = value[:max_size].rstrip()
                warning = (
                    f"Customer PO was truncated to {max_size} characters "
                    f"to fit the field."
                )
        return value, (remark_text or None), warning

    def add_product_to_order(
        self,
        order_ref: str,
        product_spec: str,
        customer_po: str = None,
        pattern_hint: str = None,
        line_remark: str = None,
        customer_width_cm=None,
    ) -> str:
        """
        Add a product line to an existing (draft) order. Meter specs use the
        meter price, yard specs the yard price.

        Args:
            order_ref: Order NAME (e.g. "SO00123"), not numeric ID
            product_spec: e.g. "227707:3m", "227707:2y"
            customer_po: Customer PO number (optional)
            line_remark: per-line remark (optional, e.g. "一個標", "急單").
                Stored TOGETHER with customer_po in the line's customer_po
                field ("PO remark"); the line remarks field is reserved for
                internal use, never customer input.
            pattern_hint: pattern/design the user mentioned (e.g. "checks");
                appends PATTERN MISMATCH WARNING on mismatch (warns only)
            customer_width_cm: width in cm the customer stated (門幅, e.g.
                150); appends WIDTH MISMATCH WARNING on mismatch with the
                system width (warns only — line is still added)

        Returns:
            Result string
        """
        try:
            from ar_chatbot.scripts.lib.odoo_client import OdooClient as OdooClientLib
            from ar_chatbot.scripts.order import parse_product_spec

            client = self._get_system_client()

            # Resolve order_ref to order_id (support both name like "26030081" and numeric ID)
            order_ref_str = str(order_ref).strip()

            # Try to find order by name first
            orders = client._call(
                model="sale.order",
                method="search_read",
                args=[[("name", "=", order_ref_str)]],
                kwargs={"fields": ["id", "name", "state"]},
            )
            if orders:
                order_id = orders[0]["id"]
            else:
                # Try as numeric ID
                try:
                    order_id = int(order_ref_str)
                    order = client.get_order(order_id)
                    if not order:
                        return f"\n[ERROR] Order '{order_ref}' not found"
                except (ValueError, TypeError):
                    return f"\n[ERROR] Order '{order_ref}' not found"

            # Check if order is editable (draft state only)
            order = client.get_order(order_id)
            if not order:
                return f"\n[ERROR] Order '{order_ref}' not found"

            # Portal/unbound access gate (2026-09-27 cross-customer leak fix)
            access_error = self._unbound_channel_error() or self._check_portal_order_access(order_id)
            if access_error:
                return f"\n[ERROR] {access_error}"

            state = order.get("state", "")
            order_name = order.get("name", f"#{order_id}")

            if state != "draft":
                if state == "sent":
                    return f"\n[ERROR] Order {order_name} is in 'Quotation Sent' state. Modifications are not allowed after confirmation."
                elif state == "sale":
                    return f"\n[ERROR] Order {order_name} is already confirmed (Sale Order). Cannot modify."
                elif state == "cancel":
                    return f"\n[ERROR] Order {order_name} has been cancelled."
                else:
                    return f"\n[ERROR] Order {order_name} is in '{state}' state. Only draft orders can be modified."

            # Parse product spec
            try:
                spec = parse_product_spec(product_spec)
            except ValueError as e:
                return f"\n[ERROR] Invalid product spec '{product_spec}': {e}"

            # Factory-number spec codes ("499.893/2:2m") must resolve via the
            # factory-code matcher BEFORE any product lookup — digit-stripping
            # ("499893") would add a wrong product line to the order.
            factory_notice = None
            raw_code = spec.get("raw_code") or spec["code"]
            if classify_code_input(raw_code) == "factory_code":
                ar_code, factory_notice, factory_error = (
                    self._resolve_factory_spec_code(raw_code)
                )
                if factory_error:
                    return f"\n[LINE_NOT_ADDED: factory_code_unresolved]\n{factory_error}"
                spec["code"] = ar_code

            # Query product
            product = client.search_product_by_code(spec["code"])
            if not product:
                return f"\n[LINE_NOT_ADDED: product_not_found]\nProduct not found: {spec['code']}"

            product_id = product["id"]
            unit = spec.get("unit", "y")
            qty_raw = float(spec["qty"])
            # Order quantities must be multiples of 0.05; round up, never down.
            qty = round_up_qty(qty_raw)
            qty_adjusted = qty != qty_raw
            product_name = product.get("name")
            product_code = spec["code"]
            line_price = spec.get("price")
            line_discount = spec.get("discount", 0)

            # ========== Pattern Mismatch Detection ==========
            pattern_warning = None
            if pattern_hint and product_name:
                mismatch = self._check_pattern_mismatch(pattern_hint, product_name)
                if mismatch:
                    pattern_warning = mismatch

            # ========== Customer-Width Mismatch Detection (warn only) ==========
            width_warning = self._check_width_mismatch(customer_width_cm, product)

            # ========== Minimum Order Quantity Check (per-brand) ==========
            moq_val, moq_unit, MIN_QTY_M, MIN_QTY_YD = self._get_moq_for_product(
                product
            )
            moq_label = f"{moq_val}{moq_unit}"

            if unit == "m" and qty < MIN_QTY_M:
                return (
                    f"\n[LINE_NOT_ADDED: moq]"
                    f"\n[ERROR] 數量低於最小採購量。"
                    f"您要求的數量為 {qty}m（約 {qty / 0.9144:.4f}yd），"
                    f"最小採購量為 {moq_label}（≈ {MIN_QTY_YD:.4f}yd / {MIN_QTY_M:.4f}m）。"
                    f"\nMinimum order quantity is {moq_label} (≈ {MIN_QTY_YD:.4f}yd / {MIN_QTY_M:.4f}m). "
                    f"Your requested quantity ({qty}m) is below the minimum."
                )
            elif unit == "y" and qty < MIN_QTY_YD:
                return (
                    f"\n[LINE_NOT_ADDED: moq]"
                    f"\n[ERROR] 數量低於最小採購量。"
                    f"您要求的數量為 {qty}yd（約 {qty * 0.9144:.4f}m），"
                    f"最小採購量為 {moq_label}（≈ {MIN_QTY_YD:.4f}yd / {MIN_QTY_M:.4f}m）。"
                    f"\nMinimum order quantity is {moq_label} (≈ {MIN_QTY_YD:.4f}yd / {MIN_QTY_M:.4f}m). "
                    f"Your requested quantity ({qty}yd) is below the minimum."
                )

            # ========== Price and UOM Selection ==========
            uom_id = None
            qty_for_odoo = qty

            if unit == "m":
                line_price = (
                    product.get("list_price", 0) if line_price is None else line_price
                )
                price_unit_label = "/m"
                uom = client.get_uom_by_name("M")
                uom_id = uom["id"] if uom else None
                display_qty_m = qty
                display_qty_yd = qty / 0.9144
            else:
                line_price = (
                    product.get(
                        "secondary_uom_list_price", product.get("list_price", 0)
                    )
                    if line_price is None
                    else line_price
                )
                price_unit_label = "/yd"
                uom = client.get_uom_by_name("Y")
                uom_id = uom["id"] if uom else None
                display_qty_yd = qty
                display_qty_m = qty * 0.9144

            # ========== Stock Status Check ==========
            stock_info = OdooClientLib.calculate_stock_status(product)

            if not stock_info["can_order"]:
                return "\n[LINE_NOT_ADDED: sold_out]\n" + self._unorderable_stock_error(
                    stock_info
                )

            stock_warning_lines = []
            if stock_info["guidance_message"]:
                stock_warning_lines.append(f"\n[INFO] {stock_info['guidance_message']}")

            if stock_info["status"] == "low_stock":
                stock_warning_lines.append(f"\n{stock_info['confirmation_message']}")
                stock_warning_lines.append(
                    "This item requires manual follow-up before the order can proceed."
                )
                result = "\n[LINE_NOT_ADDED: low_stock]\n" + "\n".join(
                    stock_warning_lines
                )
                if pattern_warning:
                    result += f"\n\n{pattern_warning}"
                return self._append_soft_triggers(result, ["low_stock"])

            # (sold_out is unreachable here: it fails the can_order check above
            # — out-of-stock products cannot be ordered, no backorders.)

            # ========== Stock Quantity Check ==========
            qty_available_yd = product.get("qty_available", 0)
            virtual_available_yd = product.get("virtual_available", 0)
            allow_out_of_stock = product.get("allow_out_of_stock_order", False)

            if allow_out_of_stock:
                stock_warning_lines.append(
                    "\n[OK] Product allows out-of-stock orders, quantity check bypassed"
                )
                stock_warning_lines.append(
                    f"  Product: [{product_code}] {product_name}"
                )
                stock_warning_lines.append(
                    f"  Required: {display_qty_yd:.4f}yd ({display_qty_m:.2f}m)"
                )
                stock_warning_lines.append(
                    f"  Stock: {qty_available_yd:.4f}yd ({qty_available_yd * 0.9144:.2f}m)"
                )
            else:
                if unit == "m":
                    required_yd = qty / 0.9144
                else:
                    required_yd = qty

                if required_yd > qty_available_yd:
                    lines = [
                        "\n[LINE_NOT_ADDED: insufficient_stock]",
                        "\n[!] Insufficient stock!",
                    ]
                    lines.append(f"  Product: [{product_code}] {product_name}")
                    lines.append(
                        f"  Required: {display_qty_yd:.4f}yd ({display_qty_m:.2f}m)"
                    )
                    lines.append(
                        f"  Stock: {qty_available_yd:.4f}yd ({qty_available_yd * 0.9144:.2f}m)"
                    )
                    lines.append(
                        f"  Virtual: {virtual_available_yd:.4f}yd ({virtual_available_yd * 0.9144:.2f}m)"
                    )
                    lines.append(
                        "\n[x] Cannot add order line. Contact warehouse or adjust quantity."
                    )
                    return "\n".join(lines)
                else:
                    stock_warning_lines.append("\n[OK] Stock quantity check passed")
                    stock_warning_lines.append(
                        f"  Product: [{product_code}] {product_name}"
                    )
                    stock_warning_lines.append(
                        f"  Required: {display_qty_yd:.4f}yd ({display_qty_m:.2f}m)"
                    )
                    stock_warning_lines.append(
                        f"  Stock: {qty_available_yd:.4f}yd ({qty_available_yd * 0.9144:.2f}m)"
                    )

            # ========== Narrow Fabric Check ==========
            narrow_info = OdooClientLib.check_narrow_fabric_status(product)
            if narrow_info["is_narrow"]:
                stock_warning_lines.append(
                    f"\n[WARNING] {narrow_info['warning_message']}"
                )
                stock_warning_lines.append(
                    "[AUTO-CONFIRMED] Proceeding with narrow fabric order."
                )

            # Customer PO text (PO number + ordinary remark) is stored in the
            # line's customer_po field; label instructions (正/反/标/標) are
            # routed to the line's remarks field.
            po_field_value, label_remarks, po_warning = self._route_line_customer_po(
                client, customer_po, line_remark
            )

            # Add order line
            line_id = client.add_order_line(
                order_id,
                product_id,
                qty_for_odoo,
                line_price,
                uom_id=uom_id,
                customer_po=po_field_value,
            )
            label_remark_warning = None
            if label_remarks:
                # Best-effort: the line IS already added — a remark-write
                # failure must not read as "nothing was added" (kimi A3)
                try:
                    client.write_order_line_remarks(
                        line_id, remarks=self._mark_customer_remark(label_remarks)
                    )
                except Exception as e:
                    logger.error(
                        f"[OdooTools] add_product_to_order: label remark write failed: {e}"
                    )
                    label_remark_warning = (
                        f"Label remark could not be saved ({e}). The order "
                        "line itself was added — retry the remark with "
                        "add_order_note."
                    )

            # Apply discount if any
            if line_discount:
                client._call(
                    model="sale.order.line",
                    method="write",
                    args=[[line_id], {"discount": line_discount}],
                )

            # Calculate subtotal
            subtotal = qty_for_odoo * line_price * (1 - line_discount / 100)

            # Build output
            lines = list(stock_warning_lines)
            lines.append("\nOrder line added:")
            lines.append(f"  Order ID: {order_id}")
            lines.append(f"  Product: {spec['code']} - {product.get('name')}")
            # Order unit FIRST (same rationale as get_price_quote: yards-first
            # made the model relabel meter orders as "yd" in summaries).
            if unit == "m":
                lines.append(
                    f"  Quantity: {display_qty_m:.2f}m ({display_qty_yd:.4f}yd)"
                )
            else:
                lines.append(
                    f"  Quantity: {display_qty_yd:.4f}yd ({display_qty_m:.2f}m)"
                )
            if qty_adjusted:
                unit_label = "m" if unit == "m" else "yd"
                lines.append(
                    f"  [INFO] 數量已向上調整為 0.05 的倍數："
                    f"{_fmt_qty(qty_raw)}{unit_label} → {_fmt_qty(qty)}{unit_label} "
                    f"(rounded up to the nearest 0.05)"
                )
            lines.append(f"  Unit Price: HK$ {line_price:.2f}{price_unit_label}")
            if line_discount > 0:
                original_subtotal = qty_for_odoo * line_price
                savings = original_subtotal - subtotal
                lines.append(f"  Discount: {line_discount}% (save HK$ {savings:.2f})")
            else:
                lines.append("  Discount: 0% (no discount applied)")
            lines.append(f"  Subtotal: HK$ {subtotal:.2f}")
            if po_field_value:
                lines.append(f"  Customer PO: {po_field_value}")
            if label_remarks:
                lines.append(f"  Label Remarks: {label_remarks}")
                if label_remark_warning:
                    lines.append(f"  [WARNING] {label_remark_warning}")
            if po_warning:
                lines.append(f"  [WARNING] {po_warning}")
            if factory_notice:
                lines.append(f"  {factory_notice}")

            # Invalidate caches affected by adding product
            self._cache.invalidate("product:")
            self._cache.invalidate("stock:")
            self._cache.invalidate("quote:")

            result = "\n".join(lines)

            # Append pattern mismatch warning (does NOT block the order)
            if pattern_warning:
                result += f"\n\n{pattern_warning}"

            # Append width mismatch warning (does NOT block the order)
            if width_warning:
                result += f"\n\n{width_warning}"

            result = self._append_soft_triggers(
                result,
                self._collect_order_soft_triggers(stock_info, display_qty_m),
            )

            # Check for mixed units and factory order triggers
            soft_triggers = self._check_order_soft_triggers(client, order_id)
            for tag in soft_triggers:
                result += f"\n[SOFT_TRIGGERS:{tag}]"

            # Lines changed: keep the already-selected carrier's price in sync
            result += self._shipping_recalc_tag(
                self._refresh_delivery_line_price(client, order_id)
            )

            return result
        except Exception as e:
            logger.error(f"[OdooTools] add_product_to_order error: {e}")
            return f"Error: {str(e)}"

    @staticmethod
    def _read_courier_tracking(client, order_id: int) -> str:
        """Read sale.order.courier_tracking (運單號, set by ops after dispatch).

        Returns "" when unset or unreadable — callers omit the line entirely
        so the agent never reports an empty tracking number.
        """
        try:
            rows = client._call(
                model="sale.order",
                method="read",
                args=[[order_id]],
                kwargs={"fields": ["courier_tracking"]},
            )
            return ((rows or [{}])[0].get("courier_tracking") or "").strip()
        except Exception as exc:
            logger.warning(
                f"[OdooTools] courier_tracking read failed for {order_id}: {exc}"
            )
            return ""

    def verify_order(self, order_ref: str) -> str:
        """
        Verify order by checking all line items and calculations.

        IMPORTANT: ALWAYS use this AFTER adding products to ensure the order
        is correct. This will:
        - Show all products with quantities, prices, and discounts
        - Verify each line's subtotal calculation
        - Verify the total amount matches the sum of line items

        Args:
            order_ref: Order Reference (e.g., "SO00123") - use order NAME, not numeric ID

        Returns:
            Verification report with [OK] or [FAIL] for each line

        Example output:
            [1] Product Name
                Quantity: 2.0000yd (1.83m)
                Unit Price: HK$ 339.02
                Discount: 22%
                Subtotal: HK$ 528.87
                Verify: 528.87 = 2.0 × 339.02 × (1 - 22%)
                [OK] Passed
                Shipping: 1 working day (現貨 in-stock)

        Each line carries its lead time (現貨 vs 訂料) and the report ends
        with an order-level "Estimated dispatch" line.
        """
        try:
            client = self._get_system_client()

            # Resolve order_ref to order_id (includes portal/unbound access
            # gate — order data must not leak across customers)
            order_id, order_name = self._resolve_order_ref(order_ref)
            if order_id is None:
                return f"\n[ERROR] {order_name}"

            # Use OdooClient.verify_order()
            result = client.verify_order(order_id)
            if not result:
                return f"\nOrder {order_ref} not found"

            # Format output matching script output
            lines = []
            lines.append("\n" + "=" * 70)
            lines.append(f"Order Verification: {order_id}")
            lines.append("=" * 70)
            lines.append(f"\nTotal Amount: HK$ {result['order_total']:.2f}")

            lines.append(f"\nOrder Lines ({len(result['lines'])} line(s)):")
            lines.append("-" * 70)

            # Lead-time per product (現貨 vs 訂料) so dispatch-time answers
            # come from Odoo data, never from example ranges in the agent's
            # instructions (2026-09-20: bot told an all-in-stock customer
            # the order was 訂料 shipping in 3-5 working days).
            line_pid_map: dict[int, int] = {}
            shipping_by_pid: dict[int, int] = {}
            try:
                id_rows = client._call(
                    model="sale.order.line",
                    method="search_read",
                    args=[[("order_id", "=", order_id)]],
                    kwargs={"fields": ["product_id"], "limit": 200},
                )
                for row in id_rows or []:
                    pid_val = row.get("product_id")
                    if isinstance(pid_val, list) and pid_val:
                        line_pid_map[row["id"]] = pid_val[0]
                unique_pids = list(set(line_pid_map.values()))
                if unique_pids:
                    prod_rows = client._call(
                        model="product.product",
                        method="read",
                        args=[unique_pids],
                        kwargs={"fields": ["id", "shipping_days_min"]},
                    )
                    for p in prod_rows or []:
                        shipping_by_pid[p["id"]] = p.get("shipping_days_min") or 0
            except Exception as exc:
                logger.warning(
                    f"[OdooTools] verify_order shipping lookup failed: {exc}"
                )

            for i, lr in enumerate(result["lines"], 1):
                product_name = lr["product_name"]
                qty = lr["qty"]
                price = lr["price"]
                discount = lr["discount"]
                subtotal = lr["subtotal"]

                # Determine UOM display (same logic as script verify_order)
                # Since OdooClient.verify_order doesn't return UOM info directly,
                # we need to read it from the line
                line_data = client._call(
                    model="sale.order.line",
                    method="read",
                    args=[[lr["line_id"]]],
                    kwargs={"fields": ["product_uom"]},
                )
                product_uom = (
                    line_data[0].get("product_uom", [None, ""])
                    if line_data
                    else [None, ""]
                )
                uom_name = (
                    product_uom[1] if product_uom and len(product_uom) > 1 else ""
                )
                uom_name_lower = uom_name.lower() if uom_name else ""

                is_meter = any(kw in uom_name_lower for kw in ["meter", "米", "m"])
                is_yard = (
                    any(kw in uom_name_lower for kw in ["yard", "码", "yd"])
                    or uom_name_lower == "y"
                )

                if is_meter:
                    qty_m = qty
                    qty_yd = qty / 0.9144
                    qty_display = f"{qty_yd:.4f}yd ({qty_m:.2f}m)"
                elif is_yard:
                    qty_yd = qty
                    qty_m = qty * 0.9144
                    qty_display = f"{qty_yd:.4f}yd ({qty_m:.2f}m)"
                else:
                    qty_display = f"{qty:.4f} ({uom_name or 'unknown unit'})"

                lines.append(f"\n[{i}] {product_name}")
                lines.append(f"    Quantity: {qty_display}")
                lines.append(f"    Unit Price: HK$ {price:.2f}")
                if discount > 0:
                    original_subtotal = qty * price
                    savings = original_subtotal - subtotal
                    lines.append(f"    Discount: {discount}% (save HK$ {savings:.2f})")
                else:
                    lines.append(f"    Discount: {discount}%")
                lines.append(f"    Subtotal: HK$ {subtotal:.2f}")
                lines.append(
                    f"    Verify: {subtotal:.2f} = {qty} x {price} x (1 - {discount}%)"
                )

                if lr["passed"]:
                    lines.append("    [OK] Passed")
                else:
                    lines.append(f"    [FAIL] Diff: {lr['diff']:.2f}")

                lead_days = shipping_by_pid.get(line_pid_map.get(lr["line_id"], 0), 0)
                if lead_days == 1:
                    lines.append("    Shipping: 1 working day (現貨 in-stock)")
                elif lead_days >= 3:
                    lines.append(
                        f"    Shipping: {lead_days}-{lead_days + 2} working days"
                        " (訂料 factory order)"
                    )

            # Show delivery/shipping lines if present
            delivery_lines = client._call(
                model="sale.order.line",
                method="search_read",
                args=[[("order_id", "=", order_id), ("is_delivery", "=", True)]],
                kwargs={"fields": ["name", "price_unit", "price_subtotal"]},
            )
            if delivery_lines:
                lines.append(f"\nShipping ({len(delivery_lines)} line(s)):")
                lines.append("-" * 70)
                for dl in delivery_lines:
                    dl_name = dl.get("name", "Delivery")
                    dl_price = dl.get("price_unit", 0)
                    dl_subtotal = dl.get("price_subtotal", 0)
                    lines.append(
                        f"  {dl_name}: HK$ {dl_price:.2f} × 1 = HK$ {dl_subtotal:.2f}"
                    )

            # Order-level dispatch estimate from the longest lead item; the
            # agent quotes this line verbatim when asked about tracking
            # numbers / dispatch time instead of inventing a range.
            fabric_lead_days = [d for d in shipping_by_pid.values() if d > 0]
            if fabric_lead_days:
                longest_lead = max(fabric_lead_days)
                if longest_lead == 1:
                    lines.append(
                        "\nEstimated dispatch: 1 working day (all items 現貨 in-stock)"
                    )
                else:
                    lines.append(
                        f"\nEstimated dispatch: {longest_lead}-{longest_lead + 2}"
                        " working days (longest lead time among items)"
                    )

            # 運單號 set by ops once the order ships — when present it
            # supersedes the dispatch estimate as the tracking answer.
            courier_tracking = self._read_courier_tracking(client, order_id)
            if courier_tracking:
                lines.append(f"\nCourier Tracking (運單號): {courier_tracking}")

            lines.append("\n" + "=" * 70)
            lines.append(f"Total: HK$ {result['order_total']:.2f}")
            if result["passed"]:
                lines.append("[OK] Verification passed")
            else:
                lines.append(
                    f"[FAIL] Verification failed! Diff: {result['total_diff']:.2f}"
                )
            # Re-emit the link marker so a later linkless turn can copy it
            # (incident 2026-08-20: confirm turn fabricated "https://...")
            if order_name:
                lines.append(f"[ORDER_LINK:{order_name}]")

            return "\n".join(lines)
        except Exception as e:
            logger.error(f"[OdooTools] verify_order error: {e}")
            return f"Error: {str(e)}"

    def confirm_order(
        self,
        order_id: str,
        auto_select_address: bool = False,
        selected_address_id: Optional[int] = None,
    ) -> str:
        """
        Confirm a sales order.

        Args:
            order_id: Order ID
            auto_select_address: If True, auto-select first address when multiple exist
            selected_address_id: Specific address ID to use (overrides auto-selection)

        Returns:
            Confirmation result string
        """
        try:
            client = self._get_system_client()
            order_id_int = int(order_id)

            # Get order information
            order = client._call(
                model="sale.order",
                method="read",
                args=[[order_id_int]],
                kwargs={"fields": ["partner_id", "partner_shipping_id", "state"]},
            )

            if not order:
                return f"\nOrder {order_id} not found"

            o = order[0]

            # Portal/unbound access gate — confirming another customer's
            # order must be denied just like reading it
            access_error = self._unbound_channel_error() or self._check_portal_order_access(order_id_int)
            if access_error:
                return f"\n{access_error}"

            # Check if order is already confirmed
            if o.get("state") not in ["draft", "sent"]:
                return (
                    f"\nOrder {order_id} is already confirmed or cancelled "
                    f"(state: {o.get('state')})"
                )

            # Address confirmation flow
            partner_id = o.get("partner_id")
            if partner_id and isinstance(partner_id, list):
                partner_id = partner_id[0]

            shipping_id = o.get("partner_shipping_id")
            if shipping_id and isinstance(shipping_id, list):
                shipping_id = shipping_id[0]

            # Get customer delivery addresses
            addresses = client.get_customer_addresses(partner_id)
            delivery_addresses = [a for a in addresses if a.get("type") == "delivery"]

            # If order already has a valid shipping address, use it
            shipping_id_to_use = None
            if shipping_id and shipping_id != partner_id:
                address_is_valid = any(
                    a.get("id") == shipping_id for a in delivery_addresses
                )
                if address_is_valid:
                    shipping_id_to_use = shipping_id
                else:
                    return (
                        f"\n[WARNING] Previously set shipping address (ID: {shipping_id}) "
                        f"is no longer valid\n"
                        f"[INFO] Please select a new shipping address"
                    )

            if delivery_addresses and shipping_id_to_use is None:
                if len(delivery_addresses) == 1:
                    addr = delivery_addresses[0]
                    shipping_id_to_use = addr.get("id")
                else:
                    # Multiple addresses
                    lines = []
                    lines.append(
                        f"\nCustomer has {len(delivery_addresses)} shipping addresses:"
                    )
                    lines.append("=" * 60)
                    for i, addr in enumerate(delivery_addresses, 1):
                        lines.append(f"\n[{i}] {addr.get('name', 'N/A')}")
                        lines.append(f"    Address ID: {addr.get('id')}")
                        lines.append(f"    {self._format_address_inline(addr)}")
                        addr_phone = self._clean_contact_phone(addr)
                        if addr_phone:
                            lines.append(
                                f"    Contact: {addr.get('name', 'N/A')} | Tel: {addr_phone}"
                            )
                    lines.append("\n" + "=" * 60)

                    if selected_address_id is not None:
                        selected_addr = next(
                            (
                                a
                                for a in delivery_addresses
                                if a.get("id") == selected_address_id
                            ),
                            None,
                        )
                        if selected_addr:
                            shipping_id_to_use = selected_addr.get("id")
                            lines.append(
                                f"\n[OK] Using selected address: "
                                f"{selected_addr.get('name')} (ID: {selected_address_id})"
                            )
                        else:
                            lines.append(
                                f"\n[ERROR] Selected address ID {selected_address_id} "
                                f"not found in delivery addresses"
                            )
                            return "\n".join(lines)
                    elif auto_select_address:
                        shipping_id_to_use = delivery_addresses[0].get("id")
                        lines.append(
                            f"\n[WARNING] Auto-selecting first address: "
                            f"{delivery_addresses[0].get('name')} "
                            f"(ID: {delivery_addresses[0].get('id')})"
                        )
                    else:
                        lines.append(
                            f"\n[ACTION REQUIRED] Multiple delivery addresses found."
                        )
                        lines.append(
                            f"Please ask the customer to select their shipping "
                            f"address (1-{len(delivery_addresses)})."
                        )
                        return "\n".join(lines)

            # Update order address if needed
            if shipping_id_to_use and shipping_id_to_use != shipping_id:
                client.update_order(
                    order_id=order_id_int,
                    partner_shipping_id=shipping_id_to_use,
                    partner_invoice_id=shipping_id_to_use,
                )

            # Confirm the order
            client.confirm_order(order_id_int)
            result = f"\nOrder {order_id} confirmed successfully"

            # Update context entry state to 'sent'
            if self.context_manager:
                try:
                    entries = self.context_manager.db.get_entries(
                        self.context_manager.user_id,
                        self.context_manager.session_id,
                        entity_type="order",
                    )
                    for entry in entries:
                        if entry.entity_id == order_id_int:
                            entry.metadata["state"] = "sent"
                            self.context_manager.db.upsert_entry(entry)
                            break
                except Exception:
                    pass  # non-critical; don't fail confirmation

            triggered = self._trigger_order_tasks(order_id)
            if triggered:
                result += f"\n[SOFT_TRIGGERS:{','.join(triggered)}]"

            # Product labels (牌仔) are NOT rendered here — on-demand only
            # (2026-10-03): the customer asks the AI to (re)send labels, or
            # 客服 triggers generate_product_labels via AI mode with a
            # confirm-and-send draft. See generate_product_labels.
            return result

        except Exception as e:
            logger.error(f"[OdooTools] confirm_order error: {e}")
            return f"Error: {str(e)}"

    # ==================== Product Labels (牌仔) ====================

    # Odoo report (custom_reports_artextile_jsi): "Product Labels" print
    # action on sale.order — one 29×42mm tag per unprinted order line.
    LABEL_REPORT_XML_ID = "custom_reports_artextile_jsi.report_artextile_product_label"
    LABEL_PNG_DPI = 300

    def _render_product_label_pngs(self, client, order_id_int: int) -> list[str]:
        """Render the Odoo Product Labels report to PNG files for one order.

        Uses the report HTTP route (``/report/pdf/...``) with the client's
        authenticated session — the JSON-RPC escape hatch cannot return the
        binary PDF cleanly. The QWeb template only renders lines with
        ``is_label_printed=False`` and marks each rendered line
        ``is_label_printed=True`` as a side effect; per product decision
        (2026-09-18) AI-generated labels revert that flag back to False so
        CS can still print from the Odoo UI. ``no_of_label_printed`` keeps
        counting (an AI render is still a render).

        Best-effort: any failure logs and returns [] — label generation must
        never fail the order confirmation itself.

        Returns:
            Absolute PNG file paths (one per unprinted order line); empty
            list when the order has no label-pending lines or on failure.
        """
        try:
            # Mirror the report's line filter EXACTLY (only is_label_printed)
            # so the revert below covers exactly the lines the template
            # marks printed during this render.
            lines = client._call(
                model="sale.order.line",
                method="search_read",
                args=[
                    [["order_id", "=", order_id_int], ["is_label_printed", "=", False]]
                ],
                kwargs={
                    "fields": ["id", "product_default_code"],
                    "order": "id ASC",
                },
            )
            if not lines:
                return []

            url = (
                f"{client.url.rstrip('/')}/report/pdf/"
                f"{self.LABEL_REPORT_XML_ID}/{order_id_int}"
            )
            resp = client._opener.open(url, timeout=120)
            pdf_bytes = resp.read()
            if not pdf_bytes or pdf_bytes[:4] != b"%PDF":
                logger.warning(
                    f"[OdooTools] label report for order {order_id_int} did "
                    f"not return a PDF ({len(pdf_bytes or b'')} bytes)"
                )
                return []

            import pymupdf

            label_dir = os.path.join(
                tempfile.gettempdir(), "ar_chatbot_labels", str(order_id_int)
            )
            os.makedirs(label_dir, exist_ok=True)
            paths = []
            with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
                for i, page in enumerate(doc, start=1):
                    line = lines[i - 1] if i <= len(lines) else None
                    code = line.get("product_default_code") if line else None
                    fallback = f"line{line['id'] if line else i}"
                    stem = re.sub(r"[^0-9A-Za-z_-]", "", str(code or "")) or fallback
                    png_path = os.path.join(label_dir, f"label_{i:02d}_{stem}.png")
                    page.get_pixmap(dpi=self.LABEL_PNG_DPI).save(png_path)
                    paths.append(png_path)

            # Revert the template's is_label_printed side effect so the
            # Odoo UI print queue stays intact (see docstring).
            line_ids = [ln["id"] for ln in lines]
            client._call(
                model="sale.order.line",
                method="write",
                args=[line_ids, {"is_label_printed": False}],
            )
            logger.info(
                f"[OdooTools] rendered {len(paths)} product label(s) for "
                f"order {order_id_int}; reverted is_label_printed on "
                f"{len(line_ids)} line(s)"
            )
            return paths
        except Exception as e:
            logger.error(
                f"[OdooTools] product label rendering failed for order "
                f"{order_id_int}: {e}"
            )
            return []

    def generate_product_labels(self, order_ref: str) -> str:
        """Generate product label (牌仔) images for a sale order.

        Renders the Odoo "Product Labels" report (29×42mm tags: customer PO,
        product code, qty, customer short name, remarks) to PNG images for
        order lines that have not been label-printed yet. 客服-only
        (2026-10-07): never triggered automatically by order confirmation
        and never available in customer conversations — call only when an
        agent in AI mode asks to print labels ("打印牌仔", "print label").

        Args:
            order_ref: Order NAME (e.g. "S01234") or numeric ID

        Returns:
            Summary text plus a LABEL_IMAGES_JSON section. The JSON section
            is system metadata consumed by the channel to deliver the images
            as native image messages — NEVER relay it to the customer.
        """
        try:
            order_id_int, order_name = self._resolve_order_ref(order_ref)
            if order_id_int is None:
                return f"Error: {order_name}"

            access_error = self._check_portal_order_access(order_id_int)
            if access_error:
                return f"Error: {access_error}"

            client = self._get_system_client()
            paths = self._render_product_label_pngs(client, order_id_int)
            if not paths:
                return (
                    f"\nNo label-pending lines on order {order_name} — every "
                    f"line was already label-printed, so no new labels were "
                    f"generated."
                )

            result = (
                f"\n[OK] Generated {len(paths)} product label image(s) for "
                f"order {order_name}."
            )
            result += "\nLABEL_IMAGES_JSON:\n" + json.dumps({"files": paths})
            return result
        except Exception as e:
            logger.error(f"[OdooTools] generate_product_labels error: {e}")
            return f"Error: {str(e)}"

    def _trigger_order_tasks(self, order_id: str) -> list[str]:
        """Deprecated compatibility shim.

        Runtime Lark task creation has been disabled in the current flow.
        """
        logger.info("Order runtime task creation is disabled")
        return []

    def cancel_order(self, order_ref: str) -> str:
        """
        Cancel an entire order.

        Args:
            order_ref: Order Reference (e.g., "SO00123") - use order NAME, not numeric ID

        Returns:
            Cancellation result string
        """
        try:
            client = self._get_system_client()
            order_id_int, order_name = self._resolve_order_ref(order_ref)
            if order_id_int is None:
                return f"\n[ERROR] {order_name}"

            order = client.get_order(order_id_int)
            if not order:
                return f"\n[ERROR] Order {order_ref} not found"

            state = order.get("state", "")
            order_name = order.get("name", f"#{order_id_int}")

            if state == "sent":
                return (
                    f"\n[ERROR] Order {order_name} is in 'Quotation Sent' state. "
                    f"Cancellation is not allowed after confirmation."
                )
            elif state == "sale":
                return (
                    f"\n[ERROR] Order {order_name} is already confirmed (Sale Order). "
                    f"Cannot cancel directly."
                )
            elif state == "cancel":
                return f"\n[ERROR] Order {order_name} has been cancelled."
            elif state != "draft":
                return (
                    f"\n[ERROR] Order {order_name} is in '{state}' state. "
                    f"Only draft orders can be cancelled."
                )

            client.cancel_order(order_id_int)
            # Invalidate stock cache since cancellation releases reserved stock
            self._cache.invalidate("stock:")
            return f"\nOrder {order_ref} cancelled"
        except Exception as e:
            logger.error(f"[OdooTools] cancel_order error: {e}")
            return f"Error: {str(e)}"

    def cancel_order_lines(self, order_ref: str, line_ids: str) -> str:
        """
        Cancel specific order lines (partial cancellation).

        Args:
            order_ref: Order Reference (e.g., "SO00123") - use order NAME, not numeric ID
            line_ids: Comma-separated order line IDs to cancel (e.g., "227701,227707")

        Returns:
            Cancellation result string
        """
        try:
            client = self._get_system_client()
            order_id_int, order_name = self._resolve_order_ref(order_ref)
            if order_id_int is None:
                return f"\n[ERROR] {order_name}"

            # Check if order is editable (draft state only)
            order = client.get_order(order_id_int)
            if not order:
                return f"\n[ERROR] Order {order_ref} not found"

            state = order.get("state", "")
            order_name = order.get("name", f"#{order_id_int}")

            if state != "draft":
                return f"\n[ERROR] Order {order_name} is not in draft state (current: {state})"

            # Parse comma-separated line IDs
            line_id_list = [int(x.strip()) for x in line_ids.split(",") if x.strip()]
            if not line_id_list:
                return "\nNo valid line IDs provided"

            # Every requested line must belong to the resolved order —
            # foreign line IDs must not be removable through a
            # gate-passing order (2026-09-28)
            line_rows = client._call(
                model="sale.order.line",
                method="read",
                args=[line_id_list],
                kwargs={"fields": ["order_id"]},
            )
            line_order = {}
            for row in line_rows or []:
                lo = row.get("order_id")
                line_order[row["id"]] = (
                    lo[0] if isinstance(lo, (list, tuple)) and lo else lo
                )
            foreign = [
                str(lid) for lid in line_id_list if line_order.get(lid) != order_id_int
            ]
            if foreign:
                return (
                    f"\n[ERROR] Line(s) {', '.join(foreign)} do not belong to "
                    f"order {order_name}"
                )

            lines = [f"\nCancelling order lines on order {order_name}..."]
            success_count = 0
            for lid in line_id_list:
                try:
                    client.remove_order_line(lid)
                    lines.append(f"  Cancelled line {lid}")
                    success_count += 1
                except Exception as e:
                    lines.append(f"  Failed to cancel line {lid}: {e}")

            lines.append(f"\n{success_count}/{len(line_id_list)} order lines cancelled")

            # Invalidate stock cache since cancellation releases reserved stock
            self._cache.invalidate("stock:")

            # Lines changed: keep the already-selected carrier's price in sync
            recalc = self._refresh_delivery_line_price(client, order_id_int)
            if recalc:
                lines.append(self._shipping_recalc_tag(recalc).strip())

            return "\n".join(lines)
        except Exception as e:
            logger.error(f"[OdooTools] cancel_order_lines error: {e}")
            return f"Error: {str(e)}"

    def update_order_line_qty(self, line_id: str, qty: float) -> str:
        """
        Update order line quantity.

        Args:
            line_id: Order line ID to update
            qty: New quantity

        Returns:
            Update result string
        """
        try:
            client = self._get_system_client()
            line_id_int = int(line_id)

            # Get current line info
            line = client._call(
                model="sale.order.line",
                method="read",
                args=[[line_id_int]],
                kwargs={"fields": ["product_id", "product_uom_qty", "order_id"]},
            )

            if not line:
                return f"\nOrder line {line_id} not found"

            l = line[0]
            product = (
                l.get("product_id", [None, ""])[1] if l.get("product_id") else "N/A"
            )
            old_qty = l.get("product_uom_qty", 0)
            order_id = l.get("order_id", [None])[0] if l.get("order_id") else None

            if not order_id:
                return f"\nCould not determine order for line {line_id}"

            # Get order info
            order = client.get_order(order_id)
            if not order:
                return f"\nOrder {order_id} not found"

            # Portal/unbound access gate — qty changes on another
            # customer's line must be denied (2026-09-27 leak fix)
            access_error = self._unbound_channel_error() or self._check_portal_order_access(order_id)
            if access_error:
                return f"\n{access_error}"

            state = order.get("state", "")
            order_name = order.get("name", f"#{order_id}")

            # Update quantity (multiples of 0.05 only; round up, never down)
            qty_raw = float(qty)
            qty = round_up_qty(qty_raw)
            client.update_order_line(line_id_int, qty=qty)

            lines = [
                f"\nOrder line {line_id} updated:",
                f"  Product: {product}",
                f"  Quantity: {old_qty} -> {qty}",
                f"  Order: {order_name} ({state})",
            ]
            if qty != qty_raw:
                lines.append(
                    f"  [INFO] 數量已向上調整為 0.05 的倍數："
                    f"{_fmt_qty(qty_raw)} → {_fmt_qty(qty)} "
                    f"(rounded up to the nearest 0.05)"
                )

            # Invalidate caches affected by quantity change
            self._cache.invalidate("product:")
            self._cache.invalidate("quote:")

            # Lines changed: keep the already-selected carrier's price in sync
            recalc = self._refresh_delivery_line_price(client, order_id)
            if recalc:
                lines.append(self._shipping_recalc_tag(recalc).strip())

            return "\n".join(lines)
        except Exception as e:
            logger.error(f"[OdooTools] update_order_line_qty error: {e}")
            return f"Error: {str(e)}"

    def set_order_line_customer_po(self, line_id: str, customer_po: str) -> str:
        """
        Set customer PO number for an order line.

        Use this when customer provides PO information after order creation.
        Customer PO is a reference number provided by the customer, such as
        "GZX0000205", "ZSX0000110", etc.

        Args:
            line_id: Order line ID (numeric)
            customer_po: Customer PO Number (e.g., "GZX0000205", "ZSX0000110")

        Returns:
            Result string
        """
        try:
            client = self._get_system_client()
            line_id_int = int(line_id)

            # Get order line info first
            line = client._call(
                model="sale.order.line",
                method="read",
                args=[[line_id_int]],
                kwargs={"fields": ["product_id", "order_id"]},
            )

            if not line:
                return f"\nOrder line {line_id} not found"

            l = line[0]
            product = (
                l.get("product_id", [None, "N/A"])[1] if l.get("product_id") else "N/A"
            )
            order_id = l.get("order_id", [None])[0] if l.get("order_id") else None
            if not order_id:
                return f"\nCould not determine order for line {line_id}"

            # Portal/unbound access gate (2026-09-28: this line-scoped write
            # path previously resolved no ownership at all)
            access_error = self._unbound_channel_error() or self._check_portal_order_access(order_id)
            if access_error:
                return f"\n{access_error}"

            # Route by the label-remark heuristic: label instructions
            # (正/反/标/標) go to the line's remarks field, the rest to
            # customer_po.
            po_part, label_part, _ = self._route_line_customer_po(
                client, customer_po, None
            )
            if po_part or not (customer_po or "").strip():
                client.update_order_line(line_id_int, customer_po=po_part or "")
            if label_part:
                client.write_order_line_remarks(
                    line_id_int, remarks=self._mark_customer_remark(label_part)
                )

            lines = [
                f"\nCustomer PO set for order line {line_id}:",
                f"  Product: {product}",
            ]
            if po_part:
                lines.append(f"  Customer PO: {po_part}")
            if label_part:
                lines.append(f"  Label Remarks: {label_part}")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"[OdooTools] set_order_line_customer_po error: {e}")
            return f"Error: {str(e)}"

    def add_order_note(
        self,
        order_ref: str,
        note: str,
        target: str = "order",
        line_id: Optional[str] = None,
    ) -> str:
        """Add a note/remark to an order or order line.

        Use when customer mentions: "Label", "label request", "備註", "remark",
        "note", "特殊要求", "加急", "特殊處理"

        Permission:
        - Portal users: only their own orders, order must be in draft state
        - Internal users: any draft order

        Args:
            order_ref: Order reference (e.g. "SO00123")
            note: Note content to append
            target: "order" (x_studio_so_remarks) or "line" (customer_po /
                remarks, routed by the label-remark heuristic)
            line_id: Required when target="line"

        Returns:
            Success/failure message
        """
        try:
            client = self._get_system_client()
            order_id_int, order_name = self._resolve_order_ref(order_ref)

            if not order_id_int:
                # order_name carries the resolution/access error verbatim
                return f"Error: {order_name}"

            # Fetch order data for permission/state checks
            order_data = client.get_order_remarks(order_id_int)

            # Portal permission: only own orders
            if self.is_portal_user():
                restricted_id = self.get_restricted_customer_id()
                if not restricted_id:
                    return "Error: Cannot determine your customer account. Please contact support."
                order_partner = order_data.get("partner_id")
                if isinstance(order_partner, list):
                    order_partner = order_partner[0]
                if order_partner != restricted_id:
                    return "Error: Portal users can only add notes to their own orders."

            # Only draft orders can have remarks added
            order_state = order_data.get("state", "")
            if order_state not in ("draft", "sent"):
                return (
                    f"\n订单 {order_ref} 已确认（状态: {order_state}），无法添加备注。"
                    f"如需特殊处理请联系销售。"
                )

            if target == "line":
                if not line_id:
                    return "Error: line_id is required when target is 'line'"

                line_id_int = int(line_id)

                # Verify line belongs to this order
                line_data = client._call(
                    "sale.order.line",
                    "read",
                    [[line_id_int]],
                    {"fields": ["order_id", "customer_po", "remarks"]},
                )
                if not line_data:
                    return f"Order line {line_id} not found"
                line_order_id = line_data[0].get("order_id")
                if isinstance(line_order_id, list):
                    line_order_id = line_order_id[0]
                if line_order_id != order_id_int:
                    return f"Error: Line {line_id} does not belong to order {order_ref}"

                # Route by the label-remark heuristic: label instructions
                # (正/反/标/標) append to the line's remarks field; other
                # customer text appends to customer_po.
                po_note, label_note = self._split_label_remark_text(note)
                if not po_note and not label_note:
                    return "Error: empty note."

                # Pre-flight the customer_po constraints BEFORE any write so a
                # missing/full field never surfaces after the remarks append
                # already landed (kimi A3: partial write reported as failure).
                po_new_value = None
                truncated = False
                if po_note:
                    field_meta = client.get_field_type(
                        "sale.order.line", "customer_po"
                    )
                    if not field_meta:
                        return (
                            "Error: Line customer_po field not available in this environment."
                        )
                    try:
                        max_size = int(field_meta.get("size") or 0)
                    except (TypeError, ValueError):
                        max_size = 0

                    # Append to existing (char field: check length)
                    existing = line_data[0].get("customer_po") or ""
                    separator = " " if existing else ""
                    new_value = existing + separator + po_note
                    if max_size and len(new_value) > max_size:
                        # Truncate to fit
                        available = max_size - len(existing) - len(separator)
                        if available <= 0:
                            return f"Error: Line Customer PO already at max length ({max_size} chars)."
                        original_note = po_note
                        po_note = po_note[:available].rstrip()
                        new_value = (existing + separator + po_note).rstrip()
                        truncated = len(po_note) < len(original_note)
                    po_new_value = new_value

                # Each field writes independently: a failure in one must not
                # un-report the other (kimi A3)
                written, failed = [], []
                if po_note:
                    try:
                        client.update_order_line(
                            line_id_int, customer_po=po_new_value
                        )
                        written.append("已记入 Customer PO 字段")
                    except Exception as e:
                        logger.error(
                            f"[OdooTools] add_order_note: customer_po write failed: {e}"
                        )
                        failed.append(f"Customer PO 写入失败：{e}")
                if label_note:
                    try:
                        existing_remarks = line_data[0].get("remarks") or ""
                        remarks_sep = " " if existing_remarks else ""
                        client.write_order_line_remarks(
                            line_id_int,
                            remarks=(
                                existing_remarks
                                + remarks_sep
                                + self._mark_customer_remark(label_note)
                            ).strip(),
                        )
                        written.append("已记入 Label Remarks 字段")
                    except Exception as e:
                        logger.error(
                            f"[OdooTools] add_order_note: remarks write failed: {e}"
                        )
                        failed.append(f"Label Remarks 写入失败：{e}")

                if not written:
                    return f"Error: 备注均未写入（{'; '.join(failed)}）。"
                truncation_msg = "\n备注过长，已自动截断。" if truncated else ""
                out = (
                    f"\n备注已添加到订单行 {line_id}（{'，'.join(written)}）。"
                    f"{truncation_msg}"
                )
                if failed:
                    out += (
                        "\n[WARN] "
                        + "；".join(failed)
                        + "（其余部分已保存，可单独重试失败部分）"
                    )
                return out

            else:  # target == "order"
                # Check if x_studio_so_remarks exists
                field_meta = client.get_field_type("sale.order", "x_studio_so_remarks")
                if not field_meta:
                    # Fallback to standard note field
                    order_remarks = client.get_order_remarks(order_id_int)
                    existing_note = order_remarks.get("note") or ""
                    new_note = (existing_note + "\n" + note) if existing_note else note
                    client.update_order_note(order_id_int, new_note)
                    return (
                        f"\n备注已记录到订单 {order_ref}（通用备注字段）。"
                        f"\n内容: {note}"
                    )

                # Append to existing x_studio_so_remarks (char field: check length)
                order_remarks = client.get_order_remarks(order_id_int)
                existing = order_remarks.get("x_studio_so_remarks") or ""
                max_size = field_meta.get("size", 255)
                new_value = (existing + "\n" + note) if existing else note

                if len(new_value) > max_size:
                    available = max_size - len(existing) - 1
                    if available <= 0:
                        return f"Error: SO remarks already at max length ({max_size} chars)."
                    note = note[:available]
                    new_value = existing + "\n" + note
                    truncation_warning = "（部分内容因长度限制已截断）"
                else:
                    truncation_warning = ""

                client.write_order_remarks(order_id_int, so_remarks=new_value)
                return (
                    f"\n备注已添加到订单 {order_ref}。{truncation_warning}"
                    f"\n内容: {note}"
                )

        except Exception as e:
            logger.error(f"[OdooTools] add_order_note error: {e}")
            return f"Error: {str(e)}"

    def _get_last_order_shipping_id(self, partner_id: int) -> Optional[int]:
        """Get shipping address ID from the most recent confirmed order.

        Query order: sale → done. Return None if neither found.
        Never uses state=None to avoid matching cancelled/draft orders.
        """
        client = self._get_system_client()

        orders = client.search_orders(partner_id, state="sale", limit=1)
        if not orders:
            orders = client.search_orders(partner_id, state="done", limit=1)
        if not orders:
            return None

        order_detail = client._call(
            model="sale.order",
            method="read",
            args=[[orders[0].get("id")]],
            kwargs={"fields": ["partner_shipping_id"]},
        )
        if order_detail:
            sid = order_detail[0].get("partner_shipping_id")
            if sid:
                return sid[0] if isinstance(sid, list) else sid
        return None

    def _is_plus_address(self, name: str) -> bool:
        return bool(name and "+" in name)

    def _sort_addresses(
        self, all_addresses, latest_shipping_id, latest_shipping_street, partner_id
    ):
        """Sort addresses: recently used first (if any), then + > main > others."""
        has_recent = (
            latest_shipping_id is not None or latest_shipping_street is not None
        )

        if has_recent:
            latest_addr = None
            plus_addrs = []
            main_addr = None
            other_addrs = []

            for addr in all_addresses:
                addr_id = addr.get("id")
                street = addr.get("street", "")
                is_latest = False
                if latest_shipping_street and street == latest_shipping_street:
                    is_latest = True
                elif latest_shipping_id and addr_id == latest_shipping_id:
                    is_latest = True

                if is_latest:
                    latest_addr = addr
                elif addr.get("id") == partner_id:
                    main_addr = addr
                elif self._is_plus_address(addr.get("name", "")):
                    plus_addrs.append(addr)
                else:
                    other_addrs.append(addr)

            result = [latest_addr] if latest_addr else []
            result += plus_addrs
            if main_addr:
                result.append(main_addr)
            result += other_addrs
            return result
        else:
            plus_addrs = []
            main_addr = None
            other_addrs = []
            for addr in all_addresses:
                if addr.get("id") == partner_id:
                    main_addr = addr
                elif self._is_plus_address(addr.get("name", "")):
                    plus_addrs.append(addr)
                else:
                    other_addrs.append(addr)
            return plus_addrs + ([main_addr] if main_addr else []) + other_addrs

    def list_customer_addresses(self, customer: str) -> str:
        """
        List all shipping addresses for a customer.

        IMPORTANT: The output displays BOTH a sequence number [1], [2], [3] AND
        the actual "Address ID" that must be used for set_order_address.

        Output format:
            [1] Address Name
                Address ID: 17282    <-- THIS is the ID to use for set_order_address
                Address: street, city...
                Contact: name | Tel: phone   <-- only when a phone is on file

        The Contact line carries the courier contact person + phone; relay
        them to the customer so they can verify before picking (2026-09-27).
        Keep the "Address ID:" line immediately followed by the "Address:"
        line — channels/whatsapp parses that adjacency.

        Args:
            customer: Customer ID or name keyword

        Returns:
            List of addresses with their actual Address IDs

        Example workflow:
            1. Call list_customer_addresses() to see available addresses
            2. Find the "Address ID" (NOT the sequence number [1], [2], etc.)
            3. Call set_order_address(order_id, address_id=ACTUAL_ID)
        """
        try:
            client = self._get_system_client()

            # Access gate (2026-09-27 cross-customer leak fix): portal users
            # are pinned to their own partner (requested customer ignored);
            # unbound channel users must bind first; internal CS unaffected.
            if self.is_portal_user():
                restricted_id = self.get_restricted_customer_id()
                if not restricted_id:
                    return "Error: Cannot determine your customer account. Please contact support."
                partner_id = restricted_id
            else:
                unbound_error = self._unbound_channel_error()
                if unbound_error:
                    return f"Error: {unbound_error}"

                # Resolve customer: try numeric ID first, then search by name
                partner_id = None
                if customer.isdigit():
                    partner_id = int(customer)
                else:
                    customers = client.search_customer(customer, limit=1)
                    if customers:
                        partner_id = customers[0]["id"]

            if not partner_id:
                return f"Customer '{customer}' not found"

            all_addresses = client.get_customer_addresses(partner_id)

            # Get main customer address
            main_customer = client._call(
                model="res.partner",
                method="read",
                args=[[partner_id]],
                kwargs={
                    "fields": ["name", "street", "street2", "city", "type", "phone", "mobile"]
                },
            )

            main_addr_street = None
            if main_customer and len(main_customer) > 0:
                c = main_customer[0]
                main_addr_street = c.get("street")
                if main_addr_street:
                    main_addr = {
                        "id": partner_id,
                        "name": c.get("name", "N/A") + " (Main)",
                        "street": c.get("street"),
                        "street2": c.get("street2", ""),
                        "city": c.get("city"),
                        "type": c.get("type", "contact"),
                        "phone": c.get("phone"),
                        "mobile": c.get("mobile"),
                    }
                    all_addresses.insert(0, main_addr)

            if not all_addresses:
                return f"\nCustomer {partner_id} has no shipping addresses"

            # Determine recently used shipping address
            latest_shipping_id = None
            latest_shipping_street = None
            try:
                latest_shipping_id = self._get_last_order_shipping_id(partner_id)
                if latest_shipping_id:
                    if latest_shipping_id == partner_id:
                        latest_shipping_street = main_addr_street
                    else:
                        sub_addr = client._call(
                            model="res.partner",
                            method="read",
                            args=[[latest_shipping_id]],
                            kwargs={"fields": ["street"]},
                        )
                        if sub_addr and len(sub_addr) > 0:
                            latest_shipping_street = sub_addr[0].get("street")
            except Exception:
                pass

            # Sort addresses using unified logic
            all_addresses = self._sort_addresses(
                all_addresses, latest_shipping_id, latest_shipping_street, partner_id
            )

            # Format output
            lines = [f"\nShipping addresses for customer {partner_id}:"]
            if latest_shipping_id:
                lines.append(f"(Recently used address: ID {latest_shipping_id})")
            lines.append("=" * 60)

            for i, addr in enumerate(all_addresses, 1):
                addr_type = addr.get("type", "other") or "other"
                name = addr.get("name") or "N/A"
                street = addr.get("street") or ""
                street2 = addr.get("street2") or ""
                city = addr.get("city") or "N/A"
                state = addr.get("state_id") or ""
                addr_id = addr.get("id")

                badge = ""
                if i == 1 and (latest_shipping_street or latest_shipping_id):
                    badge = " [Recently Used]"

                full_address = street
                if street2:
                    full_address += " " + street2
                if city and city != "N/A":
                    full_address += ", " + city
                if state and isinstance(state, list) and len(state) > 1:
                    full_address += ", " + state[1]

                lines.append(f"\n[{i}] {badge} {name}")
                lines.append(f"    Address ID: {addr_id}")
                lines.append(f"    Address: {full_address}")
                phone = self._clean_contact_phone(addr)
                if phone:
                    lines.append(f"    Contact: {name} | Tel: {phone}")

            lines.append("\n" + "=" * 60)
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"[OdooTools] list_customer_addresses error: {e}")
            return f"Error: {str(e)}"

    def create_customer_address(
        self,
        customer: str,
        name: str,
        street: str,
        street2: Optional[str] = None,
        city: Optional[str] = None,
        zip: Optional[str] = None,
        phone: Optional[str] = None,
        address_type: str = "delivery",
    ) -> str:
        """Create a new address for a customer (when not in their existing
        list — "送到新地址"/"new address"). Check list_customer_addresses()
        first; after creating, apply with set_order_address().

        Args:
            customer: Customer ID or name
            name: Contact/label for this address (e.g. "李小姐+")
            street: Street address line 1
            street2: Street address line 2 (optional)
            city: City (optional)
            zip: Postal code (optional)
            phone: Contact phone for the courier (optional but ask for it —
                it is shown on the quotation's delivery line and the address
                list so the customer can verify it)
            address_type: "delivery" (default) or "invoice"

        Returns:
            Created address ID and summary, or error message
        """
        try:
            client = self._get_system_client()

            # Portal permission: portal users can only create addresses for themselves
            if self.is_portal_user():
                restricted_partner_id = self.get_restricted_customer_id()
                if not restricted_partner_id:
                    return "Error: Cannot determine your customer account. Please contact support."
                # Resolve customer param and verify it matches the portal user's partner_id
                if customer.isdigit():
                    requested_partner_id = int(customer)
                else:
                    customers = client.search_customer(customer, limit=1)
                    requested_partner_id = customers[0]["id"] if customers else None

                if requested_partner_id != restricted_partner_id:
                    return "Error: Portal users can only create addresses for their own account."

                partner_id = restricted_partner_id
            else:
                # Unbound channel users must bind first (2026-09-28); bound
                # internal users resolve the requested customer as before.
                unbound_error = self._unbound_channel_error()
                if unbound_error:
                    return f"Error: {unbound_error}"

                # Internal user: resolve customer
                partner_id = None
                if customer.isdigit():
                    partner_id = int(customer)
                else:
                    customers = client.search_customer(customer, limit=1)
                    if customers:
                        partner_id = customers[0]["id"]

            if not partner_id:
                return f"Customer '{customer}' not found"

            # Create the address via odoo_client
            new_address_id = client.create_address(
                partner_id=partner_id,
                name=name,
                street=street,
                street2=street2,
                city=city,
                zip=zip,
                phone=phone,
                address_type=address_type,
            )

            result_lines = [
                f"\nAddress created successfully:",
                f"  Address ID: {new_address_id}",
                f"  Name: {name}",
                f"  Street: {street}",
            ]
            if street2:
                result_lines.append(f"  Street2: {street2}")
            if city:
                result_lines.append(f"  City: {city}")
            if zip:
                result_lines.append(f"  Zip: {zip}")
            if phone:
                result_lines.append(f"  Phone: {phone}")
            result_lines.append(f"  Type: {address_type}")
            result_lines.append(
                f"\nUse set_order_address(order_ref, address_id={new_address_id}) to apply to an order."
            )

            return "\n".join(result_lines)
        except Exception as e:
            logger.error(f"[OdooTools] create_customer_address error: {e}")
            return f"Error: {str(e)}"

    def _validate_address_ownership(
        self, client, order_id_int: int, address_id
    ) -> Optional[str]:
        """Guard set_order_address against non-owned address IDs.

        The address must be the order customer itself or one of its direct
        children. Menu sequence numbers ([1], [2]…) and unrelated/system
        partners are rejected with the customer's valid Address IDs so the
        caller can retry with the right ID (2026-09-15 incident: a menu "5"
        silently wrote partner #5 "Default User Template" into
        partner_shipping_id, which then matched zero shipping carriers).

        Returns None when address_id is valid; otherwise the error reply.
        """
        try:
            address_id = int(address_id)
        except (TypeError, ValueError):
            return (
                f"\n[ERROR] address_id must be an integer Address ID, "
                f"got {address_id!r}. It is NOT the menu sequence number — "
                f"re-run list_customer_addresses and use its 'Address ID'."
            )

        order = client._call(
            model="sale.order",
            method="read",
            args=[[order_id_int]],
            kwargs={"fields": ["partner_id"]},
        )
        if not order:
            return f"\n[ERROR] Order #{order_id_int} not found"
        raw_customer = order[0].get("partner_id")
        customer_id = (
            raw_customer[0] if isinstance(raw_customer, (list, tuple)) else raw_customer
        )
        customer_name = (
            raw_customer[1]
            if isinstance(raw_customer, (list, tuple)) and len(raw_customer) > 1
            else f"#{customer_id}"
        )

        addr = client._call(
            model="res.partner",
            method="read",
            args=[[address_id]],
            kwargs={"fields": ["name", "parent_id"]},
        )
        if not addr:
            addr_name = "not found"
        else:
            raw_parent = addr[0].get("parent_id")
            parent_id = (
                raw_parent[0] if isinstance(raw_parent, (list, tuple)) else raw_parent
            )
            if address_id == customer_id or parent_id == customer_id:
                return None
            addr_name = addr[0].get("name") or f"#{address_id}"

        children = client.get_customer_addresses(customer_id) or []
        valid = [f"  - Address ID {customer_id}: {customer_name} (Main)"]
        for child in children:
            valid.append(
                f"  - Address ID {child['id']}: {child.get('name', '')}"
                f" ({child.get('type', 'delivery')})"
            )
        return (
            f"\n[ERROR] Address ID {address_id} ({addr_name}) does not belong "
            f"to order customer {customer_name} (#{customer_id}) — order NOT "
            f"changed. The number passed looks like a menu sequence number, "
            f"not an Address ID. Valid addresses:\n" + "\n".join(valid)
        )

    def set_order_address(self, order_ref: str, address_id: int) -> str:
        """
        Set the shipping address for an order. "改地址" ALWAYS means the
        shipping address — do not touch the invoice address unless the customer
        explicitly says "改發票地址"/"invoice address".

        Args:
            order_ref: Order NAME (e.g. "SO00123"), not numeric ID
            address_id: ACTUAL "Address ID" from list_customer_addresses
                (e.g. 17282) — NOT the [1], [2] sequence number. Must belong
                to the order's customer; anything else is rejected.

        Returns:
            Result string
        """
        try:
            client = self._get_system_client()
            order_id_int, _ = self._resolve_order_ref(order_ref)
            if order_id_int is None:
                return f"\n[ERROR] {_}"

            ownership_error = self._validate_address_ownership(
                client, order_id_int, address_id
            )
            if ownership_error:
                return ownership_error
            # Guard passed → safe to normalize (agents sometimes pass "20833")
            address_id = int(address_id)

            # Update only shipping address (not invoice address unless explicitly requested)
            result = client.update_order(
                order_id_int,
                partner_shipping_id=address_id,
            )

            if result:
                lines = [
                    f"\nOrder {order_ref} addresses updated",
                    f"  Shipping Address ID: {address_id}",
                ]
                # New address may change the zone (and thus eligibility/price)
                # of the already-selected carrier — revalidate and resync it.
                recalc = self._refresh_delivery_line_price(
                    client, order_id_int, check_address=True
                )
                if recalc:
                    lines.append(self._shipping_recalc_tag(recalc).strip())
                return "\n".join(lines)
            else:
                return f"\nSet failed"
        except Exception as e:
            logger.error(f"[OdooTools] set_order_address error: {e}")
            return f"Error: {str(e)}"

    # list_orders grouped-rendering caps: an order with at most
    # _ORDER_LIST_MAX_INLINE_LINES product lines shows its items inline
    # under the header; a longer order (e.g. a 20-fabric one) collapses to
    # a single header line with an item count. If the inline items of the
    # whole list would still exceed _ORDER_LIST_MAX_TOTAL_LINES (whale
    # customer, limit=20), every order collapses — use get_order_detail
    # for a collapsed order's lines.
    _ORDER_LIST_MAX_INLINE_LINES = 5
    _ORDER_LIST_MAX_TOTAL_LINES = 60

    @staticmethod
    def _currency_symbol(currency_field, fallback: str = "HK$") -> str:
        """Map an Odoo currency m2o field ([id, name] or name) to a symbol."""
        name = ""
        if isinstance(currency_field, list) and len(currency_field) > 1:
            name = str(currency_field[1])
        elif isinstance(currency_field, str):
            name = currency_field
        low = name.lower()
        if low.startswith("hk") or "港" in name:
            return "HK$"
        if low.startswith("cn") or "rmb" in low or "¥" in name or "人民" in name:
            return "CN¥"
        if low.startswith("eur") or "€" in name:
            return "€"
        if low.startswith("gbp") or "£" in name or "pound" in low:
            return "£"
        if low.startswith("us") or "$" in name:
            return "US$"
        return name.split()[0] if name else fallback

    @_require_binding
    def list_orders(
        self,
        customer: str,
        days: int = 60,
        state: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        """
        List customer's orders, grouped by order (last 60 days by default).

        Each order renders one header line (date, order number, state,
        amount) followed by its product lines as `n) code - qty uom`
        (quantity in the UOM used at ordering, yd or m). Orders with more
        product lines than the inline cap — or a list whose inline items
        would grow too long — collapse back to one header line per order
        with an item count; use get_order_detail(order number) for those.
        By default cancelled and draft orders are hidden (customers asking
        for "my orders" mean real orders); pass an explicit state to see them.

        Args:
            customer: Customer ID or name keyword
            days: Time range in days (default 60)
            state: Filter by state (draft/sent/sale/done/cancel); disables the
                default cancel/draft exclusion
            limit: Maximum results (default 20)

        Example:
            list_orders("10640")  # Last 60 days
            list_orders("10640", days=30, state="sale")  # Confirmed orders in last 30 days
        """
        try:
            from datetime import datetime

            client = self._get_system_client()

            # Portal user restriction: force customer_id to own partner_id
            if self.is_portal_user():
                restricted_id = self.get_restricted_customer_id()
                if not restricted_id:
                    return "Error: Cannot determine your customer account. Please contact support."
                customer = str(restricted_id)

            # Resolve customer
            partner_id = None
            if customer.isdigit():
                partner_id = int(customer)
            else:
                customers = client.search_customer(customer, limit=1)
                if customers:
                    partner_id = customers[0]["id"]

            if not partner_id:
                return f"\nCustomer '{customer}' not found"

            # Search orders using OdooClient. Without an explicit state filter,
            # hide cancelled/draft orders (2026-11-01 WeChat feedback).
            orders = client.search_orders(
                partner_id=partner_id,
                state=state,
                days=days,
                limit=limit,
                exclude_states=None if state else ["cancel", "draft"],
            )

            if not orders:
                lines = [f"\nNo orders found for customer {partner_id}"]
                if days:
                    lines.append(f"(within the last {days} days)")
                if state:
                    lines.append(f"(state: {state})")
                return "\n".join(lines)

            # Get customer name
            customer_data = client._call(
                model="res.partner",
                method="read",
                args=[[partner_id]],
                kwargs={"fields": ["name"]},
            )
            customer_name = (
                customer_data[0].get("name", "N/A") if customer_data else "N/A"
            )

            # State labels
            state_labels = {
                "draft": "Draft",
                "sent": "Sent",
                "sale": "Confirmed",
                "done": "Done",
                "cancel": "Cancelled",
            }

            # Batch-fetch product lines for every listed order (one call).
            order_ids = [o.get("id") for o in orders if o.get("id")]
            lines_by_order = {oid: [] for oid in order_ids}
            product_ids = []
            if order_ids:
                try:
                    rows = client._call(
                        model="sale.order.line",
                        method="search_read",
                        args=[[("order_id", "in", order_ids)]],
                        kwargs={
                            "fields": [
                                "order_id",
                                "product_id",
                                "product_uom_qty",
                                "product_uom",
                                "is_delivery",
                                "display_type",
                            ],
                        },
                    )
                except Exception as e:
                    logger.warning(f"[OdooTools] list_orders line fetch failed: {e}")
                    rows = []
                for row in rows or []:
                    if row.get("display_type") or row.get("is_delivery"):
                        continue
                    row_order = row.get("order_id")
                    row_order = row_order[0] if isinstance(row_order, list) else row_order
                    if row_order not in lines_by_order:
                        continue
                    lines_by_order[row_order].append(row)
                    pid = row.get("product_id")
                    if isinstance(pid, list) and pid:
                        product_ids.append(pid[0])

            codes = self._fetch_product_codes(client, product_ids) if product_ids else {}

            # Collapse the whole list when the inline items would get too long.
            inline_total = sum(
                len(v)
                for v in lines_by_order.values()
                if len(v) <= self._ORDER_LIST_MAX_INLINE_LINES
            )
            grouped = inline_total <= self._ORDER_LIST_MAX_TOTAL_LINES

            lines = []
            lines.append("\n" + "=" * 70)
            lines.append(f"Order List (Last {days} days)")
            lines.append("=" * 70)
            lines.append(f"\nCustomer: {customer_name}")
            lines.append(f"Orders found: {len(orders)}")
            lines.append("\n" + "-" * 70)

            for i, order in enumerate(orders, 1):
                order_name = order.get("name")
                order_state = order.get("state")
                amount = order.get("amount_total") or 0
                date_order = order.get("date_order", "")

                # Format date
                if date_order:
                    try:
                        dt = datetime.fromisoformat(
                            date_order.replace("T", " ").replace("Z", "")
                        )
                        date_str = dt.strftime("%Y-%m-%d")
                    except Exception:
                        date_str = str(date_order)[:10]
                else:
                    date_str = "N/A"

                state_label = state_labels.get(order_state, order_state)
                symbol = self._currency_symbol(order.get("currency_id"))
                header = f"\n[{i}] {date_str} | {order_name} | {state_label} | {symbol} {amount:,.2f}"

                order_lines = lines_by_order.get(order.get("id"), [])
                if not grouped or len(order_lines) > self._ORDER_LIST_MAX_INLINE_LINES:
                    if order_lines:
                        header += f" ({len(order_lines)} item{'s' if len(order_lines) != 1 else ''})"
                    lines.append(header)
                    continue

                lines.append(header)
                for j, ol in enumerate(order_lines, 1):
                    pid_field = ol.get("product_id")
                    pid = pid_field[0] if isinstance(pid_field, list) and pid_field else None
                    code = codes.get(pid)
                    if not code:
                        code = (
                            pid_field[1]
                            if isinstance(pid_field, list) and len(pid_field) > 1 and pid_field[1]
                            else "?"
                        )
                    qty = ol.get("product_uom_qty") or 0
                    uom = self._uom_short_label(ol.get("product_uom"))
                    qty_str = _fmt_qty(qty) + (f" {uom}" if uom else "")
                    lines.append(f"    {j}) {code} - {qty_str}")

            lines.append("\n" + "=" * 70)

            return "\n".join(lines)
        except Exception as e:
            logger.error(f"[OdooTools] list_orders error: {e}")
            return f"Error: {str(e)}"

    @_require_binding
    def list_recent_orders_for_selection(
        self,
        customer: str,
        limit: int = 3,
    ) -> str:
        """
        List recent orders for customer selection when the order reference is ambiguous.

        Returns a numbered list of the most recent orders with:
        - Order number (SO reference)
        - Order date
        - Products in the order
        - Customer PO information

        Use this tool when the customer wants to reference an order but it's unclear
        which one (e.g., "add to my order", "check my last order").
        DO NOT just ask for the order number — proactively call this tool first.

        Args:
            customer: Customer ID or name keyword
            limit: Maximum orders to return (default 3)

        Returns:
            Numbered list of recent orders with key details

        Example:
            list_recent_orders_for_selection("10640")  # Show 3 most recent orders
        """
        try:
            from datetime import datetime

            client = self._get_system_client()

            # Portal user restriction
            if self.is_portal_user():
                restricted_id = self.get_restricted_customer_id()
                if not restricted_id:
                    return "Error: Cannot determine your customer account. Please contact support."
                customer = str(restricted_id)

            # Resolve customer
            partner_id = None
            if customer.isdigit():
                partner_id = int(customer)
            else:
                customers = client.search_customer(customer, limit=1)
                if customers:
                    partner_id = customers[0]["id"]

            if not partner_id:
                return f"\nCustomer '{customer}' not found"

            # Search recent orders (last 90 days). Cancelled orders are dropped —
            # the customer cannot act on them; drafts stay because the bot's own
            # just-created quotations are the usual "add to my order" target.
            orders = client.search_orders(
                partner_id=partner_id,
                days=90,
                limit=limit,
                exclude_states=["cancel"],
            )

            if not orders:
                return "\nNo recent orders found. Please create a new order first."

            # For each order, get line details
            lines = []
            lines.append("\n" + "=" * 70)
            lines.append("Recent Orders — Please select one:")
            lines.append("=" * 70)

            for i, order in enumerate(orders, 1):
                order_id = order.get("id")
                order_name = order.get("name", "N/A")
                date_order = order.get("date_order", "")

                # Format date
                if date_order:
                    try:
                        dt = datetime.fromisoformat(
                            date_order.replace("T", " ").replace("Z", "")
                        )
                        date_str = dt.strftime("%Y-%m-%d")
                    except Exception:
                        date_str = str(date_order)[:10]
                else:
                    date_str = "N/A"

                state = order.get("state", "N/A")
                state_labels = {
                    "draft": "Draft",
                    "sent": "Sent",
                    "sale": "Confirmed",
                    "done": "Done",
                    "cancel": "Cancelled",
                }
                state_label = state_labels.get(state, state)

                lines.append(f"\n[{i}] {order_name} — {date_str} ({state_label})")

                # Get order lines for product info
                order_lines = client.get_order_lines(order_id)
                if order_lines:
                    product_parts = []
                    for ol in order_lines:
                        prod = ol.get("product_id")
                        prod_name = (
                            prod[1]
                            if isinstance(prod, list) and len(prod) > 1
                            else str(prod)
                            if prod
                            else "N/A"
                        )
                        product_parts.append(prod_name)
                    lines.append(f"    Products: {', '.join(product_parts)}")

                    # Collect unique customer POs from lines
                    customer_pos = list(
                        dict.fromkeys(
                            ol.get("customer_po")
                            for ol in order_lines
                            if ol.get("customer_po")
                        )
                    )
                    if customer_pos:
                        lines.append(f"    Customer PO: {', '.join(customer_pos)}")
                    else:
                        lines.append("    Customer PO: (none)")
                else:
                    lines.append("    Products: (empty order)")
                    lines.append("    Customer PO: (none)")

            lines.append("\n" + "=" * 70)
            lines.append(
                "Please reply with the order number (e.g., the SO number) or the list number [1], [2], [3]."
            )

            return "\n".join(lines)
        except Exception as e:
            logger.error(f"[OdooTools] list_recent_orders_for_selection error: {e}")
            return f"Error: {str(e)}"

    def get_order_detail(self, order_ref: str) -> str:
        """
        Get detailed order information.

        Includes order header and all order lines with product, quantity,
        unit price, discount, and line amount.

        Args:
            order_ref: Order Reference (e.g., "SO00123") - use order NAME, not numeric ID
                       Numeric ID also accepted but SO number is preferred

        Returns:
            Detailed order information with all line items

        Example:
            get_order_detail("SO00123")  # By Order Reference (PREFERRED)
            get_order_detail("12345")     # By numeric ID (also works)
        """
        try:
            client = self._get_system_client()

            # Resolve order_ref to order_id (includes portal/unbound access
            # gate — order data must not leak across customers)
            order_id, resolve_error = self._resolve_order_ref(order_ref)
            if order_id is None:
                return f"\n{resolve_error}"

            # Get order details
            order = client.get_order(order_id)
            if not order:
                return f"\nOrder {order_ref} not found"

            # Get order lines
            order_lines = client.get_order_lines(order_id)

            # Format output matching script show_order_detail
            lines = []
            lines.append("\n" + "=" * 70)
            lines.append(f"Order Detail: {order_id}")
            lines.append("=" * 70)

            partner = order.get("partner_id")
            partner_name = (
                partner[1]
                if partner and isinstance(partner, list)
                else (str(partner) if partner else "N/A")
            )

            lines.append(f"\nOrder #: {order.get('name')}")
            lines.append(f"Status: {order.get('state')}")
            lines.append(f"Customer: {partner_name}")
            lines.append(f"Date: {order.get('date_order')}")

            # Shipping and invoice addresses
            shipping = order.get("partner_shipping_id")
            invoice = order.get("partner_invoice_id")

            shipping_id = (
                shipping[0] if shipping and isinstance(shipping, list) else shipping
            )
            invoice_id = (
                invoice[0] if invoice and isinstance(invoice, list) else invoice
            )

            shipping_name = (
                shipping[1]
                if shipping and isinstance(shipping, list)
                else str(shipping)
                if shipping
                else "N/A"
            )
            invoice_name = (
                invoice[1]
                if invoice and isinstance(invoice, list)
                else str(invoice)
                if invoice
                else "N/A"
            )

            # Fetch address details
            def _get_address_details(addr_id):
                if not addr_id:
                    return "N/A"
                addrs = client._call(
                    model="res.partner",
                    method="read",
                    args=[[addr_id]],
                    kwargs={
                        "fields": [
                            "street",
                            "street2",
                            "city",
                            "zip",
                            "state_id",
                            "country_id",
                        ]
                    },
                )
                if addrs and len(addrs) > 0:
                    return self._format_address_inline(addrs[0])
                return "N/A"

            shipping_full = _get_address_details(shipping_id)
            invoice_full = _get_address_details(invoice_id)

            lines.append(f"\nShipping Address ({shipping_name}):")
            lines.append(f"  {shipping_full}")
            lines.append(f"\nInvoice Address ({invoice_name}):")
            lines.append(f"  {invoice_full}")

            lines.append(f"\nCustomer PO#: {order.get('client_order_ref') or 'N/A'}")
            payment_term = order.get("payment_term_id")
            payment_term_name = (
                payment_term[1]
                if payment_term and isinstance(payment_term, list)
                else (str(payment_term) if payment_term else "N/A")
            )
            lines.append(f"Payment Term: {payment_term_name}")

            courier_tracking = self._read_courier_tracking(client, order_id)
            if courier_tracking:
                lines.append(f"\nCourier Tracking (運單號): {courier_tracking}")

            lines.append(f"\nUntaxed Amount: HK$ {order.get('amount_untaxed', 0):.2f}")
            lines.append(f"Total Amount: HK$ {order.get('amount_total', 0):.2f}")

            lines.append(f"\nOrder Lines ({len(order_lines)} line(s)):")
            lines.append("-" * 70)

            for i, ol in enumerate(order_lines, 1):
                product = ol.get("product_id")
                product_name = (
                    product[1] if product and isinstance(product, list) else "N/A"
                )
                qty = ol.get("product_uom_qty", 0)
                price = ol.get("price_unit", 0)
                discount = ol.get("discount", 0)
                subtotal = ol.get("price_subtotal", 0)
                customer_po = ol.get("customer_po")

                line_id = ol.get("id")

                lines.append(f"\n[{i}] {product_name}")
                lines.append(f"    Line ID: {line_id}")
                lines.append(f"    Quantity: {qty:.4f}yd ({qty * 0.9144:.2f}m)")
                lines.append(f"    Unit Price: HK$ {price:.2f}")
                if discount > 0:
                    original_subtotal = qty * price
                    savings = original_subtotal - subtotal
                    lines.append(f"    Discount: {discount}% (save HK$ {savings:.2f})")
                else:
                    lines.append(f"    Discount: {discount}%")
                lines.append(f"    Subtotal: HK$ {subtotal:.2f}")
                if customer_po:
                    lines.append(f"    Customer PO: {customer_po}")

            lines.append("\n" + "=" * 70)

            return "\n".join(lines)
        except Exception as e:
            logger.error(f"[OdooTools] get_order_detail error: {e}")
            return f"Error: {str(e)}"

    # ==================== Quotation (reply-to-confirm flow) ====================

    @staticmethod
    def _uom_short_label(uom_field) -> str:
        """Map an Odoo product_uom field ([id, name] or name) to a short label (yd / m)."""
        name = ""
        if isinstance(uom_field, list) and len(uom_field) > 1:
            name = str(uom_field[1])
        elif isinstance(uom_field, str):
            name = uom_field
        low = name.lower()
        if "yard" in low or low == "yd" or "碼" in name:
            return "yd"
        if "met" in low or low == "m" or "米" in name:
            return "m"
        # Fallback: use the uom name's first token, or empty
        return name.split()[0] if name else ""

    @staticmethod
    def _format_quotation(
        order_name: str,
        product_lines: list,
        shipping: Optional[dict],
        total: float,
        address_str: str,
        lang: str,
        wechat_style: bool = False,
    ) -> str:
        """Pure formatter for the reply-to-confirm quotation.

        Args:
            order_name: Order reference (e.g. SO0xxxxx) — currently unused in
                body but accepted for future header use.
            product_lines: list of dicts with keys: code, description, qty,
                uom, po, remark (optional, line-level note), unit_price,
                discount, subtotal. Lines with a zero discount or an empty po
                omit the Discount/PO# row entirely (2026-09-27 display
                simplification — no "0%" / placeholder noise).
            shipping: dict with keys: label, subtotal — or None to omit.
            total: order total.
            address_str: pre-formatted delivery address line.
            lang: "zh" for Traditional Chinese, "zh-cn" for Simplified
                (WeChat), else English.
            wechat_style: WeChat folds the old "confirm shipping cost" turn
                into this single confirmation — options become
                1=submit / 2=change delivery (address+carrier) / 3=modify
                products. Other platforms keep the classic 1/2 text.

        Returns:
            Formatted quotation string ending with the selection prompt.
        """
        is_zh = lang.startswith("zh")  # zh (Traditional) and zh-cn both
        # WeChat replies in Simplified; plain "zh" keeps Traditional strings.
        zh_cn = lang == "zh-cn"

        def zh(trad: str, simp: str) -> str:
            return simp if zh_cn else trad

        lines = []

        if is_zh:
            lines.append(zh("謝謝，以下是您的報價單：", "谢谢，以下是您的报价单："))
        else:
            lines.append("Thank you. Here is the quotation:")

        idx = 0
        for pl in product_lines:
            idx += 1
            code_desc = f"{pl['code']} {pl['description']}".strip()
            lines.append(f"{idx}. {code_desc}")
            remark = (pl.get("remark") or "").strip()
            po = (pl.get("po") or "").strip()
            has_discount = bool(pl.get("discount"))
            if is_zh:
                lines.append(f"{zh('數量', '数量')}：{_fmt_qty(pl['qty'])} {pl['uom']}")
                if po:
                    lines.append(f"PO#: {po}")
                if remark:
                    lines.append(f"{zh('備註', '备注')}：{remark}")
                lines.append(f"{zh('單價', '单价')}：HK${pl['unit_price']:,.2f}/{pl['uom']}")
                if has_discount:
                    lines.append(f"{zh('折扣', '折扣')}：{pl['discount']}%")
                lines.append(f"{zh('小計', '小计')}：HK${pl['subtotal']:,.2f}")
            else:
                lines.append(f"Qty: {_fmt_qty(pl['qty'])} {pl['uom']}")
                if po:
                    lines.append(f"PO#: {po}")
                if remark:
                    lines.append(f"Remark: {remark}")
                lines.append(f"Unit Price: HK${pl['unit_price']:,.2f}/{pl['uom']}")
                if has_discount:
                    lines.append(f"Discount: {pl['discount']}%")
                lines.append(f"Subtotal: HK${pl['subtotal']:,.2f}")

        if shipping:
            idx += 1
            lines.append(f"{idx}. {shipping['label']}")
            if is_zh:
                lines.append(f"{zh('小計', '小计')}：HK${shipping['subtotal']:,.2f}")
            else:
                lines.append(f"Subtotal: HK${shipping['subtotal']:,.2f}")

        lines.append("---")
        if is_zh:
            lines.append(f"{zh('總計', '总计')}：HK${total:,.2f}")
            lines.append("")
            lines.append(f"{zh('配送地址', '配送地址')}：{address_str}")
            lines.append("")
            lines.append(zh("請選擇：", "请选择："))
            if wechat_style:
                lines.append(zh("1. 確認報價（提交）", "1. 确认报价（提交）"))
                lines.append(
                    zh(
                        "2. 更改收貨方式（重新選擇地址和承運商）",
                        "2. 更改收货方式（重新选择地址和承运商）",
                    )
                )
                lines.append(zh("3. 修改產品", "3. 修改产品"))
            else:
                lines.append(zh("1. 確認報價", "1. 确认报价"))
                lines.append(zh("2. 需要修改", "2. 需要修改"))
        else:
            lines.append(f"Total: HK${total:,.2f}")
            lines.append("")
            lines.append(f"Delivery: {address_str}")
            lines.append("")
            lines.append("Please select:")
            if wechat_style:
                lines.append("1. Confirm quotation (submit)")
                lines.append("2. Change delivery (re-pick address & carrier)")
                lines.append("3. Modify products")
            else:
                lines.append("1. Confirm quotation")
                lines.append("2. Need modifications")

        return "\n".join(lines)

    def _fetch_product_codes(self, client, product_ids: list) -> dict:
        """Batch-read product default_code. Returns {product_id: code_str}."""
        unique_ids = [pid for pid in dict.fromkeys(product_ids) if pid]
        if not unique_ids:
            return {}
        try:
            rows = client._call(
                model="product.product",
                method="read",
                args=[unique_ids],
                kwargs={"fields": ["id", "default_code"]},
            )
            return (
                {r["id"]: (r.get("default_code") or "") for r in rows} if rows else {}
            )
        except Exception as e:
            logger.warning(f"[OdooTools] _fetch_product_codes error: {e}")
            return {}

    @staticmethod
    def _clean_line_description(name: str, code: str) -> str:
        """Strip a leading '[code]' prefix from a sale.order.line name."""
        if not name:
            return ""
        cleaned = name
        if code and cleaned.startswith(f"[{code}]"):
            cleaned = cleaned[len(f"[{code}]") :].strip()
        # Generic strip of any leading [....] tag
        if cleaned.startswith("[") and "]" in cleaned:
            tag_end = cleaned.index("]")
            cleaned = cleaned[tag_end + 1 :].strip()
        return cleaned

    def build_quotation(self, order_ref: str, lang: str = "en") -> str:
        """
        Build the reply-to-confirm quotation message for an order.

        Reads the order back (lines, pricing, shipping, delivery address) and
        formats it as a bilingual quotation ending with:
            1. Confirm quotation
            2. Need modifications

        This replaces the old portal-link confirmation. The WhatsApp channel
        layer detects this tool call and registers a pending-confirmation
        state so it can intercept the customer's 1/2 reply and send reminders.

        Args:
            order_ref: Order Reference (e.g. "SO00123") or numeric ID
            lang: "zh" for Traditional Chinese, "zh-cn" for Simplified
                (WeChat customers), "en" otherwise. Pass the language the
                CUSTOMER is using — the default "en" is only a fallback,
                not a recommendation.

        Returns:
            Formatted quotation string (send verbatim to the customer).
        """
        try:
            client = self._get_system_client()

            order_id_int, _order_name = self._resolve_order_ref(order_ref)
            if order_id_int is None:
                # _order_name carries the resolution/access error verbatim
                return f"Error: {_order_name}"

            order = client.get_order(order_id_int)
            if not order:
                return f"Error: order '{order_ref}' not found"

            # Read lines with is_delivery + product_uom (richer than get_order_lines)
            line_ids = order.get("order_line", []) or []
            raw_lines = []
            if line_ids:
                raw_lines = (
                    client._call(
                        model="sale.order.line",
                        method="read",
                        args=[line_ids],
                        kwargs={
                            "fields": [
                                "id",
                                "product_id",
                                "product_uom_qty",
                                "product_uom",
                                "price_unit",
                                "discount",
                                "price_subtotal",
                                "name",
                                "customer_po",
                                "is_delivery",
                            ]
                        },
                    )
                    or []
                )

            # Line-level remarks (custom field; may not exist in every
            # environment). Read separately so a missing field never breaks
            # the whole quotation build.
            line_remarks = {}
            if line_ids:
                try:
                    remark_rows = (
                        client._call(
                            model="sale.order.line",
                            method="read",
                            args=[line_ids],
                            kwargs={"fields": ["id", "remarks"]},
                        )
                        or []
                    )
                    line_remarks = {
                        r["id"]: (r.get("remarks") or "") for r in remark_rows
                    }
                except Exception as e:
                    logger.warning(
                        f"[OdooTools] build_quotation: remarks read skipped: {e}"
                    )

            # Batch-fetch product default codes
            product_ids = [
                (
                    ol.get("product_id")[0]
                    if isinstance(ol.get("product_id"), list)
                    else ol.get("product_id")
                )
                for ol in raw_lines
            ]
            code_map = self._fetch_product_codes(client, product_ids)

            header_po = order.get("client_order_ref") or ""

            product_lines = []
            shipping = None
            for ol in raw_lines:
                pid_field = ol.get("product_id")
                pid = pid_field[0] if isinstance(pid_field, list) else pid_field
                code = code_map.get(pid, "")
                subtotal = ol.get("price_subtotal", 0) or 0

                if ol.get("is_delivery"):
                    label_en = ol.get("name") or "Shipping"
                    shipping = {"label": label_en, "subtotal": float(subtotal)}
                    continue

                # Skip zero-subtotal non-delivery note lines (e.g. section/note)
                if not pid:
                    continue

                # Empty line PO falls back to the order-level PO; when neither
                # exists the formatter omits the PO# row (no "N/A" noise).
                po = ol.get("customer_po") or header_po or ""
                product_lines.append(
                    {
                        "code": code,
                        "description": self._clean_line_description(
                            ol.get("name", ""), code
                        ),
                        "qty": float(ol.get("product_uom_qty", 0) or 0),
                        "uom": self._uom_short_label(ol.get("product_uom")),
                        "po": po,
                        # Only bot-written customer text (「」-marked) is
                        # customer-visible; unmarked remarks (sales/production
                        # entries) never render (2026-09-28 privacy fix).
                        "remark": self._extract_customer_remarks(
                            line_remarks.get(ol.get("id"), "")
                        ),
                        "unit_price": float(ol.get("price_unit", 0) or 0),
                        "discount": float(ol.get("discount", 0) or 0),
                        "subtotal": float(subtotal),
                    }
                )

            # Delivery address inline
            shipping_field = order.get("partner_shipping_id")
            shipping_id = (
                shipping_field[0]
                if isinstance(shipping_field, list)
                else shipping_field
            )
            address_str = "N/A"
            if shipping_id:
                addrs = client._call(
                    model="res.partner",
                    method="read",
                    args=[[shipping_id]],
                    kwargs={
                        "fields": [
                            "name",
                            "phone",
                            "mobile",
                            "street",
                            "street2",
                            "city",
                            "zip",
                            "state_id",
                            "country_id",
                        ]
                    },
                )
                if addrs:
                    address_str = self._compose_delivery_address_line(addrs[0], lang=lang)

            total = float(order.get("amount_total", 0) or 0)

            return self._format_quotation(
                order_name=order.get("name", ""),
                product_lines=product_lines,
                shipping=shipping,
                total=total,
                address_str=address_str,
                lang=lang,
                wechat_style=getattr(self, "platform_type", "") == "wechat",
            )
        except Exception as e:
            logger.error(f"[OdooTools] build_quotation error: {e}")
            return f"Error: {str(e)}"

    def submit_quotation_for_confirmation(
        self, order_ref: str, lang: str = "en"
    ) -> str:
        """
        Build the quotation and mark the order as awaiting the customer's 1/2
        reply (reply-to-confirm flow). Send the quotation text verbatim; the
        trailing [PENDING_QUOTATION:{...}] metadata line is parsed/stripped by
        the channel layer — do NOT relay it to the customer.

        Args:
            order_ref: Order NAME (e.g. "SO00123") or numeric ID
            lang: "zh"/"zh-cn" for Chinese (quotation header follows it),
                "en" otherwise. Pass the language the CUSTOMER is using —
                the default "en" is only a fallback, not a recommendation.

        Returns:
            Quotation string + trailing metadata tag, or an "Error:" string.
        """
        quotation = self.build_quotation(order_ref, lang=lang)
        if quotation.startswith("Error:"):
            return quotation

        client = None
        try:
            client = self._get_system_client()
            order_id_int, order_name = self._resolve_order_ref(order_ref)
            partner_id = None
            order = None
            if order_id_int:
                order = client.get_order(order_id_int)
                p = order.get("partner_id") if order else None
                partner_id = p[0] if isinstance(p, list) else p
        except Exception as e:
            logger.warning(f"[OdooTools] submit_quotation metadata resolve error: {e}")
            order_id_int, order_name, partner_id, order = None, str(order_ref), None, None

        # Balance sufficiency snapshot (客服-side only — never blocks, never
        # shown to the customer; feeds the WeChat 导航面板 余额不足需人工审核
        # node via the channel layer). Degrades to None on ANY failure so
        # channels without credit_limit_jsi (production Odoo) keep working.
        balance = self._balance_snapshot(client, partner_id, order)

        meta = json.dumps(
            {
                "order_id": order_id_int,
                "order_name": order_name,
                "partner_id": partner_id,
                # Preserve the zh-cn (Simplified/WeChat) variant — normalizing
                # it to "en" here forced English canned replies on WeChat.
                "lang": lang if lang in ("zh", "zh-cn", "en") else "en",
                "balance": balance,
            }
        )
        return f"{quotation}\n[PENDING_QUOTATION:{meta}]"

    def _balance_snapshot(self, client, partner_id, order) -> Optional[dict]:
        """Best-effort balance check for the pending-quotation meta tag.

        Returns the check result dict (state/kind/available/order_amount/
        term_name) or None when the customer's term is check-free or the
        profile read fails. Never raises — the quotation must always go out.
        """
        if not client or not partner_id or not order:
            return None
        try:
            from ar_chatbot.messaging.balance import check_balance

            profile = client.get_partner_payment_profile(partner_id)
            # dict guard: degrade (no check) on clients that lack the method
            # or return junk — the quotation must always go out.
            if not isinstance(profile, dict):
                return None
            result = check_balance(profile, float(order.get("amount_total") or 0.0))
            if result.state == "skipped":
                return None
            return {
                "state": result.state,
                "kind": result.kind,
                "available": result.available,
                "order_amount": result.order_amount,
                "term_name": result.term_name,
            }
        except Exception as e:
            logger.warning(f"[OdooTools] balance snapshot skipped: {e}")
            return None

    def submit_draft_reply_to_user(self, content: str) -> str:
        """
        Draft a customer-facing reply awaiting 客服 confirmation. Call ONLY
        when 客服 explicitly asks you to reply/tell/inform the customer; when
        merely chatting with you, reply normally (stays 客服-only).

        `content` = the EXACT customer-facing message, delivered verbatim after
        客服 confirms: no meta-commentary ("here is the draft", "內容如下"...),
        no quotes/brackets — just the message itself in the assistant's voice.

        Args:
            content: The verbatim customer-facing reply (nothing else).

        Returns:
            Content + trailing [PENDING_DRAFT_REPLY:{...}] metadata line
            (parsed and stripped by the channel layer — relay only the content).
        """
        draft_id = uuid.uuid4().hex[:12]
        meta = json.dumps({"draft_id": draft_id, "content": content})
        return f"{content}\n[PENDING_DRAFT_REPLY:{meta}]"

    @_require_binding
    def query_order_history(
        self,
        product_code: Optional[str] = None,
        customer: Optional[str] = None,
        days: int = 180,
    ) -> str:
        """Query a customer's past order lines across all orders (e.g. "Have I
        ordered 227707 before?"). Portal users: own history only (customer
        auto-detected); internal users must pass customer. Cancelled and draft
        orders are excluded — history means orders actually placed.

        Args:
            product_code: 6-digit product code; None = all lines in the period
            customer: Customer ID or name (optional for portal users)
            days: Lookback window (default 180)

        Returns:
            Order history: order number, date, product, qty (m+y), price, subtotal
        """
        logger.info(
            f"[OdooTools] query_order_history called: "
            f"product_code={product_code}, customer={customer}, days={days}"
        )

        # --- Permission & customer resolution ---
        partner_id = None

        if self.is_portal_user():
            restricted_id = self.get_restricted_customer_id()
            if not restricted_id:
                return (
                    "Error: Cannot determine your customer account. "
                    "Please contact support."
                )
            partner_id = restricted_id
        else:
            if not customer:
                return (
                    "Error: Please specify a customer ID or name "
                    "to query order history."
                )
            try:
                partner_id = int(customer)
            except (ValueError, TypeError):
                pass  # Will resolve via search below

        # --- Get OdooClient ---
        # System account architecture: always use system client
        client = self._get_system_client()

        # --- Resolve customer name to partner_id if needed ---
        if partner_id is None and customer:
            customers = client.search_customer(customer, limit=1)
            if customers:
                partner_id = customers[0]["id"]
            else:
                return f"Error: Customer '{customer}' not found."

        if not partner_id:
            return "Error: Could not determine customer ID."

        # --- Sanitize product_code ---
        if product_code:
            code = re.sub(r"^AR", "", product_code, flags=re.IGNORECASE)
            digits = re.sub(r"[^0-9]", "", code)
            product_code = digits[:6] if len(digits) >= 6 else None

        # --- Execute query ---
        try:
            lines = client.search_order_lines(
                partner_id=partner_id,
                product_code=product_code,
                days=days,
            )
        except Exception as e:
            logger.error(f"[OdooTools] search_order_lines failed: {e}")
            return f"Error querying order history: {str(e)}"

        return self._format_order_history(lines, partner_id, product_code, days)

    def _format_order_history(
        self,
        lines: List[Dict],
        partner_id: int,
        product_code: Optional[str],
        days: int,
    ) -> str:
        """Format order history lines for AI consumption."""
        from datetime import datetime

        if not lines:
            msg = f"No order history found for customer {partner_id}"
            if product_code:
                msg += f" with product {product_code}"
            msg += f" in the last {days} days."
            return msg

        header_lines = [
            "Order History Query Results",
            "=" * 60,
            f"Customer: {partner_id}",
        ]
        if product_code:
            header_lines.append(f"Product: {product_code}")
        header_lines.append(f"Period: Last {days} days")
        header_lines.append(f"Found: {len(lines)} order line(s)")
        header_lines.append("")

        result_lines = []
        total_qty_m = 0.0
        total_qty_yd = 0.0

        for i, line in enumerate(lines, 1):
            # Order info from order_id (many2one: [id, name])
            order_id_raw = line.get("order_id")
            order_name = (
                order_id_raw[1]
                if isinstance(order_id_raw, list) and len(order_id_raw) > 1
                else str(order_id_raw)
            )

            # Date
            create_date = line.get("create_date", "")
            if create_date:
                try:
                    dt = datetime.fromisoformat(
                        create_date.replace("T", " ").replace("Z", "")
                    )
                    date_str = dt.strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    date_str = str(create_date)[:10]
            else:
                date_str = "N/A"

            # Product
            code = line.get("product_default_code", "N/A")
            name = line.get("name", "N/A")
            if code != "N/A" and f"[{code}]" in name:
                name = name.split(f"[{code}]")[-1].strip()

            # UOM conversion (1 yard = 0.9144 meters)
            qty = line.get("product_uom_qty", 0)
            product_uom = line.get("product_uom")
            uom_name = (
                product_uom[1]
                if isinstance(product_uom, list) and len(product_uom) > 1
                else ""
            )
            uom_lower = uom_name.lower() if uom_name else ""

            is_meter = (
                any(kw in uom_lower for kw in ["meter", "米", "m"])
                and "mm" not in uom_lower
            )
            is_yard = any(kw in uom_lower for kw in ["yard", "码", "yd"])

            if is_meter:
                qty_m = qty
                qty_yd = qty / 0.9144
                uom_label = "/m"
            elif is_yard:
                qty_yd = qty
                qty_m = qty * 0.9144
                uom_label = "/yd"
            else:
                qty_m = qty * 0.9144
                qty_yd = qty
                uom_label = ""

            price = line.get("price_unit", 0)
            discount = line.get("discount", 0)
            subtotal = line.get("price_subtotal", 0)

            total_qty_m += qty_m
            total_qty_yd += qty_yd

            result_lines.append(f"[{i}] Order: {order_name} | Date: {date_str}")
            result_lines.append(f"    Product: {code} - {name}")
            result_lines.append(
                f"    Quantity: {qty_yd:.2f}yd ({qty_m:.2f}m) "
                f"| Unit Price: HK$ {price:.2f}{uom_label}"
            )
            if discount > 0:
                result_lines.append(
                    f"    Discount: {discount}% | Subtotal: HK$ {subtotal:.2f}"
                )
            else:
                result_lines.append(f"    Subtotal: HK$ {subtotal:.2f}")
            result_lines.append("")

        summary = (
            f"Total: {total_qty_yd:.2f}yd ({total_qty_m:.2f}m) "
            f"across {len(lines)} order(s)"
        )

        return "\n".join(header_lines + result_lines + [summary])

    @_require_binding
    def query_dye_lot_info(
        self,
        product_code: str,
        customer: Optional[str] = None,
        days: int = 365,
    ) -> str:
        """Query dye lot/batch info for a product's past orders ("配同匹",
        "same dye lot", "什麼批次"). Portal users auto-detected; internal
        users must pass customer.

        Args:
            product_code: 6-digit product code
            customer: Customer ID or name (optional for portal users)
            days: Lookback period (default 365)

        Returns:
            Dye lot info: order number, lot number, date, quantity
        """
        logger.info(
            f"[OdooTools] query_dye_lot_info called: "
            f"product_code={product_code}, customer={customer}, days={days}"
        )

        try:
            client = self._get_system_client()

            # --- Permission & customer resolution ---
            partner_id = None
            if self.is_portal_user():
                restricted_id = self.get_restricted_customer_id()
                if not restricted_id:
                    return "Error: Cannot determine your customer account. Please contact support."
                partner_id = restricted_id
            else:
                if not customer:
                    return "Error: Please specify a customer ID or name."
                if customer and customer.isdigit():
                    partner_id = int(customer)
                elif customer:
                    customers = client.search_customer(customer, limit=1)
                    if customers:
                        partner_id = customers[0]["id"]
                    else:
                        return f"Error: Customer '{customer}' not found."

            if not partner_id:
                return "Error: Could not determine customer ID."

            # --- Sanitize product_code ---
            code = re.sub(r"^AR", "", product_code, flags=re.IGNORECASE)
            digits = re.sub(r"[^0-9]", "", code)
            product_code = digits[:6] if len(digits) >= 6 else None
            if not product_code:
                return "Error: Invalid product code."

            # --- Step 1: Get order lines ---
            lines = client.search_order_lines(
                partner_id=partner_id,
                product_code=product_code,
                days=days,
            )

            if not lines:
                return f"未找到客戶 {partner_id} 購買產品 {product_code} 的歷史訂單（最近 {days} 天）。"

            # --- Step 2: Try x_studio_*lot* fields on sale.order.line ---
            lot_fields = client.get_sale_order_line_lot_fields()
            if lot_fields:
                first_lot_field = lot_fields[0]
                line_ids = [l["id"] for l in lines]
                lot_data = client._call(
                    model="sale.order.line",
                    method="read",
                    args=[line_ids],
                    kwargs={
                        "fields": ["id", first_lot_field, "order_id", "create_date"]
                    },
                )
                results = []
                if lot_data:
                    for ld in lot_data:
                        lot_val = ld.get(first_lot_field, "")
                        if lot_val:
                            order_ref = ld.get("order_id", "")
                            if isinstance(order_ref, list) and len(order_ref) > 1:
                                order_ref = order_ref[1]
                            date_str = str(ld.get("create_date", ""))[:10]
                            results.append(
                                {
                                    "order": order_ref,
                                    "lot": str(lot_val),
                                    "date": date_str,
                                }
                            )

                if results:
                    return self._format_dye_lot_results(
                        product_code, partner_id, results, source="direct field"
                    )

            # --- Step 3: Fallback via picking chain ---
            order_ids = set()
            product_id = None
            for line in lines:
                order_id_raw = line.get("order_id")
                if isinstance(order_id_raw, list) and len(order_id_raw) > 0:
                    order_ids.add(order_id_raw[0])
                pid_raw = line.get("product_id")
                if pid_raw and not product_id:
                    product_id = pid_raw[0] if isinstance(pid_raw, list) else pid_raw

            all_lot_results = []
            for oid in order_ids:
                lot_info = client.get_picking_lot_info(oid, product_id)
                for li in lot_info:
                    order_data = client._call(
                        model="sale.order",
                        method="read",
                        args=[[oid]],
                        kwargs={"fields": ["name", "date_order"]},
                    )
                    if order_data:
                        all_lot_results.append(
                            {
                                "order": order_data[0].get("name", ""),
                                "lot": li["lot_name"],
                                "date": str(order_data[0].get("date_order", ""))[:10],
                                "qty": li.get("qty_done", 0),
                            }
                        )

            if all_lot_results:
                return self._format_dye_lot_results(
                    product_code, partner_id, all_lot_results, source="picking chain"
                )

            # No lot info found
            return (
                f"產品 {product_code} 在最近 {days} 天內有 {len(lines)} 筆訂單記錄，"
                f"但暫無 dye lot / 批次資訊。"
                f"\n可能原因：訂單尚未確認發貨，或該產品未啟用批次追蹤。"
            )

        except Exception as e:
            logger.error(f"[OdooTools] query_dye_lot_info error: {e}")
            return f"Error: {str(e)}"

    def _format_dye_lot_results(
        self,
        product_code: str,
        partner_id: int,
        results: List[Dict],
        source: str,
    ) -> str:
        """Format dye lot results for AI consumption."""
        lines = [
            f"Dye Lot / Batch Info for Product {product_code}",
            f"Customer: {partner_id}",
            f"Source: {source}",
            f"Found: {len(results)} record(s)",
            "=" * 60,
        ]

        # Sort by date descending
        results.sort(key=lambda x: x.get("date", ""), reverse=True)

        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] Order: {r['order']} | Date: {r['date']}")
            lines.append(f"    Lot/Batch: {r['lot']}")
            if r.get("qty"):
                lines.append(f"    Quantity: {r['qty']}")

        # Show most recent lot prominently
        if results:
            latest = results[0]
            lines.append("")
            lines.append(f"Most recent lot: {latest['lot']} (Order {latest['order']})")

        return "\n".join(lines)

    def check_small_order(self, order_ref: str, product_code: str) -> str:
        """Safety net after EVERY add_product_to_order: detect if a product's
        total quantity in the order is <1m (catches cases early detection
        missed). If is_small_order AND the customer wasn't already asked via
        Path A, run the purpose-gate flow (two-option 用途 question; on 配色
        record the SO line remark — see the Small Orders prompt rules). Never
        transfers to a human.

        Args:
            order_ref: Order NAME (e.g. "SO00123")
            product_code: 6-digit product code

        Returns:
            JSON: is_small_order, product_code, product_name, total_qty_m,
            order_ref — or plain text when not a small order
        """
        logger.info(
            f"[OdooTools] check_small_order called: "
            f"order_ref={order_ref}, product_code={product_code}"
        )
        try:
            client = self._get_system_client()

            # Resolve order_ref to order_id
            order_ref_str = str(order_ref).strip()
            orders = client._call(
                model="sale.order",
                method="search_read",
                args=[
                    [
                        "|",
                        ("name", "=", order_ref_str),
                        (
                            "id",
                            "=",
                            int(order_ref_str) if order_ref_str.isdigit() else 0,
                        ),
                    ]
                ],
                kwargs={"fields": ["id", "name"], "limit": 1},
            )
            if not orders:
                return f"Order '{order_ref}' not found."
            order_id = orders[0]["id"]
            order_name = orders[0]["name"]

            # Portal/unbound access gate (2026-09-27 cross-customer leak fix)
            access_error = self._unbound_channel_error() or self._check_portal_order_access(order_id)
            if access_error:
                return f"Error: {access_error}"

            # Sanitize product code
            product_code = self._sanitize_product_code(product_code)

            # Find product by code
            product = client.search_product_by_code(product_code)
            if not product:
                return f"Product '{product_code}' not found."
            product_id = product["id"]
            product_name = product.get("name", product_code)

            # Query ALL order lines for this product in this order, get total quantity
            lines = client._call(
                model="sale.order.line",
                method="search_read",
                args=[
                    ["&", ("order_id", "=", order_id), ("product_id", "=", product_id)]
                ],
                kwargs={"fields": ["product_uom_qty", "product_uom"]},
            )
            if not lines:
                return f"Product '{product_code}' not found in order '{order_name}'."

            # Sum quantities, converting to meters
            total_qty_yd = 0.0
            total_qty_m = 0.0
            for line in lines:
                qty = line.get("product_uom_qty", 0)
                uom_id = line.get("product_uom")
                # Check if uom is meters or yards
                if uom_id:
                    uom_data = client._call(
                        model="uom.uom",
                        method="read",
                        args=[[uom_id[0] if isinstance(uom_id, list) else uom_id]],
                        kwargs={"fields": ["name"]},
                    )
                    if uom_data:
                        uom_name = uom_data[0].get("name", "")
                        is_meter, is_yard = self._detect_uom(uom_name)
                        if is_meter:
                            total_qty_m += qty
                            total_qty_yd += qty / 0.9144
                        else:
                            total_qty_yd += qty
                            total_qty_m += qty * 0.9144
                    else:
                        # Default: assume yards
                        total_qty_yd += qty
                        total_qty_m += qty * 0.9144
                else:
                    # Default: assume yards
                    total_qty_yd += qty
                    total_qty_m += qty * 0.9144

            if total_qty_m < 1.0:
                result = {
                    "is_small_order": True,
                    "product_code": product_code,
                    "product_name": product_name,
                    "total_qty_m": round(total_qty_m, 4),
                    "total_qty_yd": round(total_qty_yd, 4),
                    "order_ref": order_name,
                    "summary": f"下單 {product_code} {total_qty_m:.2f}m (訂單 {order_name})",
                    "message": (
                        f"Small order detected: product {product_code} quantity is "
                        f"{total_qty_m:.2f}m (< 1m). "
                        f"Please ask the customer if this is for dye lot matching (配色)."
                    ),
                }
                return json.dumps(result, ensure_ascii=False)
            else:
                return (
                    f"Product {product_code} total quantity is {total_qty_m:.2f}m "
                    f"(>= 1m). Not a small order."
                )

        except Exception as e:
            logger.error(f"[OdooTools] check_small_order error: {e}")
            return f"Error: {str(e)}"

    def create_small_order_lark_task(
        self,
        order_ref: str,
        product_code: str,
        product_name: str,
        quantity_meters: float,
        is_dye_lot: bool = False,
        is_supplement: bool = False,
        last_order_date: Optional[str] = None,
        dye_lot_info: Optional[str] = None,
    ) -> str:
        """Emit the small-order CS handoff (soft trigger) — call ONLY after the
        customer confirms 配色/補料 for a <1m order (from check_small_order
        JSON or Path A early detection). Never call when NOT 配色. After
        calling, stop the order flow (no auto-quote/creation). Idempotent per
        (order_ref, product_code) per session.

        Args:
            order_ref: Order NAME, or "PENDING" (Path A, before order creation)
            product_code: Product code (e.g. "822707")
            product_name: From check_small_order result (or the code if unknown)
            quantity_meters: Total meters from check_small_order / request
            is_dye_lot: True if customer confirmed 配色
            is_supplement: True if customer confirmed 補料
            last_order_date: Customer-provided last order date (optional)
            dye_lot_info: From query_dye_lot_info (optional)

        Returns:
            Result message with soft-trigger metadata for the channel layer
        """
        logger.info(
            f"[OdooTools] create_small_order_lark_task called: "
            f"order_ref={order_ref}, product_code={product_code}, "
            f"is_dye_lot={is_dye_lot}, is_supplement={is_supplement}"
        )

        # Idempotency check via context manager
        if self.context_manager:
            if self.context_manager.is_small_order_task_created(
                order_ref, product_code
            ):
                return (
                    f"A small order task for {product_code} in order {order_ref} "
                    f"has already been created. Skipping duplicate."
                )

        try:
            clean_code = self._sanitize_product_code(product_code)

            # Runtime Lark task creation is disabled. The channel soft trigger
            # handles the human handoff now.
            if self.context_manager:
                self.context_manager.mark_small_order_task_created(
                    order_ref, product_code
                )

            # Build structured summary for channel layer
            purpose = "配色" if is_dye_lot else ("補料" if is_supplement else "")
            summary_parts = [f"下單 {clean_code} {float(quantity_meters):.2f}m"]
            if purpose:
                summary_parts.append(f"用於{purpose}")
            if last_order_date:
                summary_parts.append(f"上次下單日期: {last_order_date}")
            summary = ", ".join(summary_parts)

            return (
                f"Small order recorded: "
                f"Product: {clean_code}, Qty: {float(quantity_meters):.2f}m. "
                f"CS staff will follow up."
                f"\n[SOFT_TRIGGERS:small_order]"
                f"\n[SUMMARY:{summary}]"
            )

        except Exception as e:
            logger.error(f"[OdooTools] create_small_order_lark_task error: {e}")
            return f"Error: {str(e)}"

    # ==================== Shipping / Delivery ====================

    @staticmethod
    def _eval_rule_condition(variable, operator, max_value, price_dict):
        """Evaluate a delivery price rule condition."""
        value = price_dict.get(variable, 0)
        if operator == "<=":
            return value <= max_value
        elif operator == "<":
            return value < max_value
        elif operator == ">=":
            return value >= max_value
        elif operator == ">":
            return value > max_value
        elif operator == "==":
            return value == max_value
        return False

    def _get_order_shipping_params(self, client, order_id):
        """Compute shipping parameters (weight, total, volume, etc.) from order lines.

        Mirrors Odoo's _get_price_available aggregation logic.
        """
        lines = client._call(
            model="sale.order.line",
            method="search_read",
            args=[[("order_id", "=", order_id), ("is_delivery", "=", False)]],
            kwargs={
                "fields": [
                    "product_id",
                    "product_uom_qty",
                    "price_subtotal",
                    "display_type",
                ]
            },
        )

        product_ids = set()
        line_items = []
        for line in lines:
            if line.get("display_type"):
                continue
            pid = line.get("product_id")
            if not pid:
                continue
            product_id = pid[0] if isinstance(pid, list) else pid
            product_ids.add(product_id)
            line_items.append(
                {
                    "product_id": product_id,
                    "qty": line.get("product_uom_qty", 0) or 0,
                    "subtotal": line.get("price_subtotal", 0) or 0,
                }
            )

        products = {}
        if product_ids:
            prods = client._call(
                model="product.product",
                method="read",
                args=[list(product_ids)],
                kwargs={"fields": ["weight", "volume", "type"]},
            )
            for p in prods:
                products[p["id"]] = p

        total = weight = volume = quantity = wv = 0.0
        for li in line_items:
            prod = products.get(li["product_id"])
            if not prod or prod.get("type") == "service":
                continue
            qty = li["qty"]
            pw = prod.get("weight", 0) or 0
            pv = prod.get("volume", 0) or 0
            weight += pw * qty
            volume += pv * qty
            wv += pw * pv * qty
            quantity += qty
            total += li["subtotal"]

        return {
            "price": total,
            "weight": weight,
            "volume": volume,
            "wv": wv,
            "quantity": quantity,
        }

    def _compute_carrier_price(
        self, client, carrier_id, carrier_data, shipping_params, rules=None
    ):
        """Compute carrier shipping price directly from pricing rules.

        For fixed type: uses carrier's fixed_price field.
        For base_on_rule: evaluates delivery.price.rule records, first match wins,
        then applies carrier margins.

        Args:
            client: OdooClient instance
            carrier_id: Carrier ID
            carrier_data: Carrier record dict (must include delivery_type, fixed_price, etc.)
            shipping_params: Dict from _get_order_shipping_params
            rules: Optional pre-fetched price rules for base_on_rule carriers
        """
        delivery_type = carrier_data.get("delivery_type", "fixed")

        if delivery_type == "fixed":
            price = carrier_data.get("fixed_price", 0) or 0
            # Check free_over
            if carrier_data.get("free_over"):
                threshold = carrier_data.get("amount", 0) or 0
                if shipping_params.get("price", 0) >= threshold:
                    price = 0.0
            return price

        elif delivery_type == "base_on_rule":
            if rules is None:
                rules = client._call(
                    model="delivery.price.rule",
                    method="search_read",
                    args=[[("carrier_id", "=", carrier_id)]],
                    kwargs={
                        "fields": [
                            "variable",
                            "operator",
                            "max_value",
                            "list_base_price",
                            "list_price",
                            "variable_factor",
                        ],
                        "order": "sequence, list_price, id",
                    },
                )

            price = None
            for rule in rules:
                variable = rule.get("variable", "weight")
                operator = rule.get("operator", "<=")
                max_value = rule.get("max_value", 0) or 0
                if self._eval_rule_condition(
                    variable, operator, max_value, shipping_params
                ):
                    base = rule.get("list_base_price", 0) or 0
                    multiplier = rule.get("list_price", 0) or 0
                    factor_value = shipping_params.get(
                        rule.get("variable_factor", "weight"), 0
                    )
                    price = base + multiplier * factor_value
                    break

            if price is None:
                return None

            # Apply margins
            margin = carrier_data.get("margin", 0) or 0
            fixed_margin = carrier_data.get("fixed_margin", 0) or 0
            price = price * (1 + margin) + fixed_margin
            return price

        return None

    def _carrier_matches_address(self, client, carrier, partner_data):
        """Replicate delivery.carrier._match_address (delivery/models/delivery_carrier.py:145).

        Each empty restriction means "no restriction". Returns True when the
        shipping address satisfies the carrier's country / state / zip-prefix
        limits. Used to filter the carrier list the same way the /shop/payment
        page does, since we cannot call the recordset-only _match_address via
        JSON-RPC.
        """
        country_ids = carrier.get("country_ids") or []
        if country_ids:
            p_country = partner_data.get("country_id")
            p_country_id = (
                p_country[0] if isinstance(p_country, (list, tuple)) else p_country
            )
            if p_country_id not in country_ids:
                return False

        state_ids = carrier.get("state_ids") or []
        if state_ids:
            p_state = partner_data.get("state_id")
            p_state_id = p_state[0] if isinstance(p_state, (list, tuple)) else p_state
            if p_state_id not in state_ids:
                return False

        zip_prefix_ids = carrier.get("zip_prefix_ids") or []
        if zip_prefix_ids:
            p_zip = (partner_data.get("zip") or "").upper()
            if not p_zip:
                return False
            prefixes = client._call(
                model="delivery.zip.prefix",
                method="read",
                args=[zip_prefix_ids],
                kwargs={"fields": ["name"]},
            )
            if not any(
                (px.get("name") or "").upper() and p_zip.startswith(px["name"].upper())
                for px in prefixes
            ):
                return False

        return True

    def _get_order_currency_symbol(self, client, order_id):
        """Get currency symbol from order's company. Returns '¥' for CNY, 'HK$' for HKD."""
        try:
            order_data = client._call(
                model="sale.order",
                method="read",
                args=[[order_id]],
                kwargs={"fields": ["company_id"]},
            )
            if order_data and order_data[0].get("company_id"):
                comp_id = order_data[0]["company_id"]
                if isinstance(comp_id, (list, tuple)):
                    comp_id = comp_id[0]
                comp_data = client._call(
                    model="res.company",
                    method="read",
                    args=[[comp_id]],
                    kwargs={"fields": ["currency_id"]},
                )
                if comp_data and comp_data[0].get("currency_id"):
                    cur = comp_data[0]["currency_id"]
                    cur_name = cur[1] if isinstance(cur, (list, tuple)) else str(cur)
                    if "HKD" in cur_name.upper():
                        return "HK$"
        except Exception as e:
            logger.error(f"[OdooTools] _get_order_currency_symbol error: {e}")
        return "¥"

    @staticmethod
    def _get_price_display(carrier_name, product_name, price, currency_symbol="¥"):
        """Determine price display: ¥XX.XX, HK$XX.XX, 運費到付, or 免運費."""
        name_upper = (carrier_name or "").upper()
        prod_upper = (product_name or "").upper()
        is_cod = (
            "COD" in name_upper
            or "COD" in prod_upper
            or "運費到付" in (product_name or "")
        )

        if is_cod:
            return "運費到付"
        elif price == 0:
            return "免運費"
        else:
            return f"{currency_symbol}{price:.2f}"

    @staticmethod
    def _carrier_sort_key(entry):
        """Sort key for shipping carriers (smaller sorts first).

        Free detection is based on price_display text, so it stays correct
        regardless of how the carrier's fixed_price is configured.

        Display priority — MUST match the agent prompt's display instruction
        so the model's numbered list uses the tool JSON order verbatim
        (incident 2026-08-19: tool order had COD first while the prompt
        grouped COD last; the user's "1" got mapped to the wrong carrier):
          0 - last-used carrier (kept on top of the price-based ordering)
          1 - paid carriers, ordered by ascending price (查詢中/unknown last)
          2 - 免運費 (free shipping)
          3 - COD 運費到付 (freight collect)
          4 - self-pickup ("自取"/"不需要物流"), always last regardless of price
        """
        name = entry.get("name", "") or ""
        display = entry.get("price_display", "") or ""

        if "自取" in name or "不需要物流" in name:
            return (4, 0.0, name)
        if entry.get("is_last_used"):
            return (0, 0.0, name)
        if display == "運費到付":
            return (3, 0.0, name)
        if display == "免運費":
            return (2, 0.0, name)

        price = entry.get("price")
        return (1, price if price is not None else float("inf"), name)

    @staticmethod
    def _carrier_available(carrier: dict) -> bool:
        """Mirror Odoo website-checkout carrier visibility.

        A carrier is offerable only when it is published (website_published)
        AND belongs to the shared scope or the pinned ODOO_COMPANY_ID —
        unpublished drafts or other companies' carriers must never reach
        customers (e.g. PRC顺丰-广东: unpublished, company 4, RMB rules).
        """
        if not carrier.get("website_published", True):
            return False
        comp = carrier.get("company_id")
        comp_id = comp[0] if isinstance(comp, (list, tuple)) else comp
        if not comp_id:
            return True
        raw = os.getenv("ODOO_COMPANY_ID")
        if not raw:
            return True
        try:
            return comp_id == int(raw)
        except (TypeError, ValueError):
            return True

    def list_shipping_carriers(self, customer: str, order_ref: str = None) -> str:
        """
        List available shipping carriers with estimated prices.

        When order_ref is provided, calculates and includes the shipping price
        for each carrier using the order's product weight and carrier pricing rules.
        The customer's last-used carrier is sorted first and marked.

        Permission Rules:
        - Portal users: pinned to their own binding partner_id (the customer
          argument is ignored — no cross-customer address leakage)
        - Internal users: Must specify customer (ID or name keyword)
        - Unbound channel users: denied with the standard binding prompt

        Args:
            customer: Customer ID or name keyword
            order_ref: Order Reference (optional). When provided, each carrier
                       includes price and price_display fields.

        Returns:
            JSON string with carriers list. Each carrier contains:
            id, name, delivery_type, is_last_used, and (if order_ref given)
            price, price_display. When the shipping address is resolved
            (order's partner_shipping_id, else the customer's own address),
            the top level also carries shipping_address (the formatted
            address the freight is computed for — prefixed with the courier
            contact + phone so the customer can verify them) +
            shipping_address_id.

        Example output (with order_ref):
            {
              "carriers": [
                {"id": 5, "name": "SF SHIPPING", "is_last_used": true,
                 "price": 92.0, "price_display": "¥92.00"},
                {"id": 3, "name": "DHL SHIPPING", "is_last_used": false,
                 "price": 125.0, "price_display": "¥125.00"}
              ],
              "shipping_address": "李小姐+ (+852 9123 4567), G/F, 188 Queen's Road West, Sai Ying Pun, Hong Kong",
              "shipping_address_id": 17282
            }
        """
        try:
            client = self._get_system_client()

            # Access gate (2026-09-28): portal users are pinned to their own
            # partner (the docstring promised auto-detect but it was never
            # implemented — a foreign customer param leaked that customer's
            # shipping address); unbound channel users must bind first.
            if self.is_portal_user():
                restricted_id = self.get_restricted_customer_id()
                if not restricted_id:
                    return json.dumps(
                        {
                            "error": "Cannot determine your customer account. "
                            "Please contact support."
                        },
                        ensure_ascii=False,
                    )
                partner_id = restricted_id
            else:
                unbound_error = self._unbound_channel_error()
                if unbound_error:
                    return json.dumps({"error": unbound_error}, ensure_ascii=False)

                # Resolve customer
                if customer and customer.isdigit():
                    partner_id = int(customer)
                else:
                    customers = client.search_customer(customer, limit=1)
                    if not customers:
                        return json.dumps(
                            {"error": f"Customer '{customer}' not found"},
                            ensure_ascii=False,
                        )
                    partner_id = customers[0]["id"]

            # Get offerable carriers: active + published + company scope
            # (mirrors the website checkout; unpublished/other-company
            # carriers are never quoted to customers)
            carrier_domain = [("active", "=", True), ("website_published", "=", True)]
            company_id_raw = os.getenv("ODOO_COMPANY_ID")
            if company_id_raw:
                try:
                    carrier_domain.append(
                        ("company_id", "in", [False, int(company_id_raw)])
                    )
                except ValueError:
                    pass
            carriers = client._call(
                model="delivery.carrier",
                method="search_read",
                args=[carrier_domain],
                kwargs={
                    "fields": [
                        "id",
                        "name",
                        "delivery_type",
                        "product_id",
                        "fixed_price",
                        "free_over",
                        "amount",
                        "margin",
                        "fixed_margin",
                        "country_ids",
                        "state_ids",
                        "zip_prefix_ids",
                        "website_published",
                        "company_id",
                    ],
                    "order": "name",
                },
            )
            # Defense in depth: re-apply availability in case the domain
            # filter is bypassed (mocks, schema drift, Odoo upgrades)
            carriers = [c for c in (carriers or []) if self._carrier_available(c)]

            if not carriers:
                return json.dumps(
                    {"carriers": [], "message": "No active carriers found"},
                    ensure_ascii=False,
                )

            # Find last-used carrier from customer's most recent confirmed order
            last_carrier_product_id = None
            last_orders = client.search_orders(partner_id, state="sale", limit=1)
            if not last_orders:
                last_orders = client.search_orders(partner_id, state="done", limit=1)

            if last_orders:
                delivery_lines = client._call(
                    model="sale.order.line",
                    method="search_read",
                    args=[
                        [
                            ("order_id", "=", last_orders[0]["id"]),
                            ("is_delivery", "=", True),
                        ]
                    ],
                    kwargs={"fields": ["product_id"], "limit": 1},
                )
                if delivery_lines:
                    pid = delivery_lines[0].get("product_id")
                    last_carrier_product_id = pid[0] if isinstance(pid, list) else pid

            # Filter carriers by shipping address (mirrors delivery.carrier._match_address).
            # Primary source: order's partner_shipping_id. Fallback: customer's own address.
            # If no address can be resolved, skip filtering (preserve previous behaviour).
            addr_partner_id = None
            if order_ref:
                addr_order_id, _ = self._resolve_order_ref(order_ref)
                if addr_order_id:
                    addr_order = client._call(
                        model="sale.order",
                        method="read",
                        args=[[addr_order_id]],
                        kwargs={"fields": ["partner_shipping_id"]},
                    )
                    if addr_order:
                        ps = addr_order[0].get("partner_shipping_id")
                        addr_partner_id = ps[0] if isinstance(ps, (list, tuple)) else ps
            if not addr_partner_id:
                addr_partner_id = partner_id
            partner_data = None
            if addr_partner_id:
                addr_data = client._call(
                    model="res.partner",
                    method="read",
                    args=[[addr_partner_id]],
                    kwargs={
                        "fields": [
                            "name",
                            "phone",
                            "mobile",
                            "street",
                            "street2",
                            "city",
                            "zip",
                            "country_id",
                            "state_id",
                        ]
                    },
                )
                if addr_data:
                    partner_data = addr_data[0]
            if partner_data:
                carriers = [
                    c
                    for c in carriers
                    if self._carrier_matches_address(client, c, partner_data)
                ]

            # The address the freight is computed for — surfaced so the model
            # can show it next to the carrier list (freight is zone-specific;
            # the customer must be able to confirm WHICH address was used).
            # Contact + phone ride along (courier-step verification,
            # 2026-09-27 user report).
            shipping_address = None
            if partner_data:
                shipping_address = self._compose_delivery_address_line(partner_data)

            # Compute shipping params if order_ref provided
            shipping_params = None
            all_rules = {}
            currency_symbol = "¥"
            if order_ref:
                order_id, _ = self._resolve_order_ref(order_ref)
                if order_id:
                    shipping_params = self._get_order_shipping_params(client, order_id)
                    currency_symbol = self._get_order_currency_symbol(client, order_id)

                    # Batch-fetch price rules for base_on_rule carriers
                    rule_carrier_ids = [
                        c["id"]
                        for c in carriers
                        if c.get("delivery_type") == "base_on_rule"
                    ]
                    if rule_carrier_ids:
                        rules = client._call(
                            model="delivery.price.rule",
                            method="search_read",
                            args=[[("carrier_id", "in", rule_carrier_ids)]],
                            kwargs={
                                "fields": [
                                    "carrier_id",
                                    "variable",
                                    "operator",
                                    "max_value",
                                    "list_base_price",
                                    "list_price",
                                    "variable_factor",
                                ],
                                "order": "sequence, list_price, id",
                            },
                        )
                        for r in rules:
                            cid = (
                                r["carrier_id"][0]
                                if isinstance(r["carrier_id"], list)
                                else r["carrier_id"]
                            )
                            all_rules.setdefault(cid, []).append(r)

            # Build entries
            result_carriers = []
            for c in carriers:
                pid = c.get("product_id")
                product_id_val = pid[0] if isinstance(pid, list) else pid
                product_name = pid[1] if isinstance(pid, list) and len(pid) > 1 else ""
                is_last_used = (
                    last_carrier_product_id is not None
                    and product_id_val == last_carrier_product_id
                )

                entry = {
                    "id": c["id"],
                    "name": c["name"],
                    "delivery_type": c.get("delivery_type", ""),
                    "is_last_used": is_last_used,
                }

                # Compute price if order_ref provided
                if shipping_params:
                    carrier_rules = all_rules.get(c["id"])
                    price = self._compute_carrier_price(
                        client, c["id"], c, shipping_params, carrier_rules
                    )
                    if price is not None:
                        entry["price"] = price
                        entry["price_display"] = self._get_price_display(
                            c["name"], product_name, price, currency_symbol
                        )
                    else:
                        entry["price"] = None
                        entry["price_display"] = "查詢中"

                result_carriers.append(entry)

            # Sort carriers by display priority:
            #   1. last-used carrier first (unless it is self-pickup)
            #   2. free carriers (price == 0, incl. COD 運費到付)
            #   3. paid carriers by ascending price
            #   4. self-pickup ("自取"/"不需要物流") always last
            result_carriers.sort(key=self._carrier_sort_key)

            result = {"carriers": result_carriers}
            if shipping_address:
                result["shipping_address"] = shipping_address
                result["shipping_address_id"] = addr_partner_id
            return json.dumps(result, ensure_ascii=False)

        except Exception as e:
            logger.error(f"[OdooTools] list_shipping_carriers error: {e}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def calculate_shipping(self, order_ref: str, carrier_id: str) -> str:
        """
        Calculate shipping cost for an order using the selected carrier.

        Computes price directly from carrier pricing rules:
        - fixed type: uses carrier's fixed_price
        - base_on_rule type: evaluates delivery.price.rule records

        Args:
            order_ref: Order Reference (e.g., "26030081")
            carrier_id: Carrier ID from list_shipping_carriers output

        Returns:
            JSON string with carrier_id, carrier_name, price, price_display.
        """
        try:
            client = self._get_system_client()
            carrier_id_int = int(carrier_id)

            order_id, order_name = self._resolve_order_ref(order_ref)
            if order_id is None:
                return json.dumps({"error": order_name}, ensure_ascii=False)

            carrier_data = client._call(
                model="delivery.carrier",
                method="read",
                args=[[carrier_id_int]],
                kwargs={
                    "fields": [
                        "id",
                        "name",
                        "product_id",
                        "delivery_type",
                        "fixed_price",
                        "free_over",
                        "amount",
                        "margin",
                        "fixed_margin",
                        "website_published",
                        "company_id",
                    ]
                },
            )
            if not carrier_data:
                return json.dumps(
                    {"error": f"Carrier {carrier_id} not found"}, ensure_ascii=False
                )

            carrier = carrier_data[0]
            carrier_name = carrier["name"]

            # Guard: never quote unpublished / other-company carriers
            if not self._carrier_available(carrier):
                return json.dumps(
                    {
                        "error": f"Carrier '{carrier_name}' is not available (unpublished or outside company scope)",
                    },
                    ensure_ascii=False,
                )
            pid = carrier.get("product_id")
            product_name = pid[1] if isinstance(pid, list) and len(pid) > 1 else ""

            shipping_params = self._get_order_shipping_params(client, order_id)
            currency_symbol = self._get_order_currency_symbol(client, order_id)
            price = self._compute_carrier_price(
                client, carrier_id_int, carrier, shipping_params
            )

            if price is None:
                return json.dumps(
                    {
                        "carrier_id": carrier_id_int,
                        "carrier_name": carrier_name,
                        "error": "No matching pricing rule found for this order",
                    },
                    ensure_ascii=False,
                )

            return json.dumps(
                {
                    "carrier_id": carrier_id_int,
                    "carrier_name": carrier_name,
                    "price": price,
                    "price_display": self._get_price_display(
                        carrier_name, product_name, price, currency_symbol
                    ),
                },
                ensure_ascii=False,
            )

        except Exception as e:
            logger.error(f"[OdooTools] calculate_shipping error: {e}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def _add_shipping_line(self, order_id: int, carrier_id: int, price: float) -> str:
        """Add a delivery line to the order via sale.order.line.create.

        Creates an is_delivery=True order line with the carrier's delivery product.
        Cannot use set_delivery_line via JSON-RPC (requires recordset).

        Returns:
            Success message or error string
        """
        client = self._get_system_client()
        try:
            # Get carrier info for product_id and name
            carrier_data = client._call(
                model="delivery.carrier",
                method="read",
                args=[[carrier_id]],
                kwargs={"fields": ["name", "product_id"]},
            )
            if not carrier_data:
                return "Error: Carrier not found"
            carrier = carrier_data[0]
            product_id = (
                carrier["product_id"][0]
                if isinstance(carrier["product_id"], (list, tuple))
                else carrier["product_id"]
            )

            client._call(
                model="sale.order.line",
                method="create",
                args=[
                    {
                        "order_id": order_id,
                        "product_id": product_id,
                        "name": carrier["name"],
                        "product_uom_qty": 1,
                        "price_unit": price,
                        "is_delivery": True,
                    }
                ],
            )
            return "OK"
        except Exception as e:
            logger.error(f"[OdooTools] _add_shipping_line error: {e}")
            return f"Error: {str(e)}"

    def _remove_shipping_lines(self, order_id: int) -> str:
        """Remove all delivery lines from the order.

        Returns:
            "OK" or error string
        """
        client = self._get_system_client()
        try:
            # Find is_delivery lines
            delivery_lines = client._call(
                model="sale.order.line",
                method="search_read",
                args=[[("order_id", "=", order_id), ("is_delivery", "=", True)]],
                kwargs={"fields": ["id"]},
            )
            if delivery_lines:
                line_ids = [l["id"] for l in delivery_lines]
                client._call(
                    model="sale.order.line",
                    method="unlink",
                    args=[line_ids],
                )
            return "OK"
        except Exception as e:
            logger.error(f"[OdooTools] _remove_shipping_lines error: {e}")
            return f"Error: {str(e)}"

    def _refresh_delivery_line_price(
        self, client, order_id: int, check_address: bool = False
    ):
        """Recalculate the delivery line price after order lines/address changed.

        Shipping prices are computed from the order's weight/total at
        carrier-selection time and written as a fixed price_unit; later line
        changes do NOT auto-update it. This recomputes the price for the
        order's current carrier and, when it differs, writes it back.

        Returns None when the order has no carrier / no delivery line (nothing
        to sync); otherwise one of:
          {"status": "unchanged", "price": float}
          {"status": "updated", "old_price": float, "new_price": float}
          {"status": "unavailable"}  # price not computable / zone mismatch
        """
        try:
            order_fields = ["carrier_id"] + (
                ["partner_shipping_id"] if check_address else []
            )
            order = client._call(
                model="sale.order",
                method="read",
                args=[[order_id]],
                kwargs={"fields": order_fields},
            )
            if not order:
                return None
            carrier_ref = order[0].get("carrier_id")
            carrier_id = (
                carrier_ref[0]
                if isinstance(carrier_ref, (list, tuple))
                else carrier_ref
            )
            if not carrier_id:
                return None

            delivery_lines = client._call(
                model="sale.order.line",
                method="search_read",
                args=[[("order_id", "=", order_id), ("is_delivery", "=", True)]],
                kwargs={"fields": ["id", "price_unit"], "limit": 1},
            )
            if not delivery_lines:
                return None
            delivery_line = delivery_lines[0]
            old_price = float(delivery_line.get("price_unit") or 0)

            carrier_fields = [
                "name",
                "delivery_type",
                "fixed_price",
                "free_over",
                "amount",
                "margin",
                "fixed_margin",
            ]
            if check_address:
                carrier_fields += ["country_ids", "state_ids", "zip_prefix_ids"]
            carrier_data = client._call(
                model="delivery.carrier",
                method="read",
                args=[[carrier_id]],
                kwargs={"fields": carrier_fields},
            )
            if not carrier_data:
                return {"status": "unavailable"}
            carrier = carrier_data[0]

            if check_address:
                ps = order[0].get("partner_shipping_id")
                ps_id = ps[0] if isinstance(ps, (list, tuple)) else ps
                partner_data = None
                if ps_id:
                    pdata = client._call(
                        model="res.partner",
                        method="read",
                        args=[[ps_id]],
                        kwargs={"fields": ["country_id", "state_id", "zip"]},
                    )
                    partner_data = pdata[0] if pdata else None
                if partner_data and not self._carrier_matches_address(
                    client, carrier, partner_data
                ):
                    return {"status": "unavailable"}

            shipping_params = self._get_order_shipping_params(client, order_id)
            new_price = self._compute_carrier_price(
                client, carrier_id, carrier, shipping_params
            )
            if new_price is None:
                return {"status": "unavailable"}

            if abs(new_price - old_price) < 0.01:
                return {"status": "unchanged", "price": old_price}

            client._call(
                model="sale.order.line",
                method="write",
                args=[delivery_line["id"], {"price_unit": new_price}],
            )
            return {"status": "updated", "old_price": old_price, "new_price": new_price}
        except Exception as e:
            logger.error(f"[OdooTools] _refresh_delivery_line_price error: {e}")
            return None

    @staticmethod
    def _shipping_recalc_tag(result) -> str:
        """Format the [SHIPPING_RECALC:...] marker for line-mutation tool output."""
        if not result:
            return ""
        status = result.get("status")
        if status == "unchanged":
            return f"\n[SHIPPING_RECALC:unchanged price={result.get('price', 0):.2f}]"
        if status == "updated":
            return (
                f"\n[SHIPPING_RECALC:updated "
                f"old={result.get('old_price', 0):.2f} "
                f"new={result.get('new_price', 0):.2f}]"
            )
        return "\n[SHIPPING_RECALC:unavailable]"

    def update_order_shipping(
        self, order_ref: str, carrier_id: str, price: float
    ) -> str:
        """
        Update shipping on an existing order: remove old delivery lines and add new one.

        Use this when the customer wants to change the carrier after shipping was
        already added to the order.

        Args:
            order_ref: Order Reference (e.g., "26030081")
            carrier_id: New carrier ID from list_shipping_carriers
            price: Shipping price from calculate_shipping result

        Returns:
            Result message
        """
        try:
            client = self._get_system_client()
            carrier_id_int = int(carrier_id)

            order_id, order_name = self._resolve_order_ref(order_ref)
            if order_id is None:
                return f"Error: {order_name}"

            # Read carrier up-front (name + delivery product + address restrictions
            # + availability fields) so we can guard the shipping address and
            # availability, and reuse the name without a 2nd read.
            carrier_data = client._call(
                model="delivery.carrier",
                method="read",
                args=[[carrier_id_int]],
                kwargs={
                    "fields": [
                        "name",
                        "product_id",
                        "country_ids",
                        "state_ids",
                        "zip_prefix_ids",
                        "website_published",
                        "company_id",
                    ]
                },
            )
            if not carrier_data:
                return f"Error: Carrier {carrier_id} not found"
            carrier = carrier_data[0]

            # Guard: unpublished / other-company carriers are never settable
            # (mirrors website checkout visibility; e.g. draft carriers with
            # unconverted RMB rules or wrong-UoM delivery products)
            if not self._carrier_available(carrier):
                return json.dumps(
                    {
                        "error": f"Carrier '{carrier['name']}' is not available (unpublished or outside company scope)",
                    },
                    ensure_ascii=False,
                )

            # Guard: reject if the carrier does not deliver to the shipping address.
            order_data = client._call(
                model="sale.order",
                method="read",
                args=[[order_id]],
                kwargs={"fields": ["partner_shipping_id"]},
            )
            if order_data:
                ps = order_data[0].get("partner_shipping_id")
                ps_id = ps[0] if isinstance(ps, (list, tuple)) else ps
                if ps_id:
                    pdata = client._call(
                        model="res.partner",
                        method="read",
                        args=[[ps_id]],
                        kwargs={"fields": ["country_id", "state_id", "zip"]},
                    )
                    partner_data = pdata[0] if pdata else None
                    if partner_data and not self._carrier_matches_address(
                        client, carrier, partner_data
                    ):
                        return json.dumps(
                            {
                                "error": f"Carrier '{carrier['name']}' does not deliver to this shipping address",
                            },
                            ensure_ascii=False,
                        )

            # Remove existing delivery lines
            rm_result = self._remove_shipping_lines(order_id)
            if rm_result.startswith("Error"):
                return rm_result

            # Add new delivery line
            add_result = self._add_shipping_line(order_id, carrier_id_int, price)
            if add_result.startswith("Error"):
                return add_result

            # Link the carrier on the order. Native set_delivery_line does this; we
            # replicate via JSON-RPC write so stock.picking and reports inherit it.
            client._call(
                model="sale.order",
                method="write",
                args=[order_id, {"carrier_id": carrier_id_int}],
            )

            return json.dumps(
                {
                    "success": True,
                    "order_ref": order_name,
                    "carrier_id": carrier_id_int,
                    "carrier_name": carrier["name"],
                    "price": price,
                    # Re-emit the link marker so the NEXT turn can still show a
                    # real link even when it runs no tools (incident 2026-08-20:
                    # confirm turn fabricated "https://...")
                    "order_link_marker": f"[ORDER_LINK:{order_name}]",
                },
                ensure_ascii=False,
            )

        except Exception as e:
            logger.error(f"[OdooTools] update_order_shipping error: {e}")
            return f"Error: {str(e)}"
