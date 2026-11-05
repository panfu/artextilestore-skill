"""Odoo JSON-RPC Client

Provides a Python interface to Odoo's JSON-RPC API for sales operations.
"""

import os
import json
import logging
import ssl
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from http.cookiejar import CookieJar
from urllib.request import build_opener, HTTPCookieProcessor
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def normalize_odoo_base_url(url: Optional[str]) -> str:
    """Strip trailing slashes and a trailing ``/web`` from an Odoo URL.

    ODOO_URL is commonly configured as the web-client URL (".../web"), but
    every consumer needs the bare instance root: JSON-RPC endpoints are
    built as ``/web/...`` themselves (a leftover ``/web`` doubles the path
    and the instance answers 404), and user-facing storefront links live at
    ``/shop/...`` (a leftover ``/web`` yields backend-only URLs like
    ``/web/shop/<code>`` that portal customers cannot open).
    """
    url = (url or "").rstrip("/")
    if url.endswith("/web"):
        url = url[: -len("/web")]
    return url


@dataclass
class AuthResult:
    """Detailed authentication result from Odoo.

    Attributes:
        success: Whether authentication was successful
        uid: User ID if successful
        requires_2fa: Whether 2FA is required for this account
        error: Human-readable error message
        error_type: Error category ('credentials', '2fa', 'network', 'ssl', 'timeout', 'unknown')
    """
    success: bool
    uid: Optional[int] = None
    requires_2fa: bool = False
    error: Optional[str] = None
    error_type: Optional[str] = None


class OdooClient:
    """Client for interacting with Odoo via JSON-RPC"""

    # User-Agent for all requests. Required for Odoo instances behind
    # Cloudflare bot protection (error 1010), which blocks urllib's default
    # "Python-urllib/x.y" signature.
    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )

    # 库存状态阈值常量
    YARD_TO_METER = 0.9144
    LOW_STOCK_THRESHOLD = 8.0  # 低库存阈值（码）
    RESTOCK_INFO_THRESHOLD = LOW_STOCK_THRESHOLD  # 显示补货信息阈值（码）
    STOCK_RESIDUE_EPSILON = 0.01  # 库存残料阈值（码）：低于此值视为无货（测量残留）
    NARROW_FABRIC_THRESHOLD_CM = 145  # 窄幅面料阈值（厘米）
    # 門幅差异提醒阈值（厘米）：客戶自報門幅與產品門幅（width_cm）相差超过此值即提醒
    WIDTH_MISMATCH_TOLERANCE_CM = 1.0

    # 大订单阈值常量
    LARGE_ORDER_METERS = 15  # 大订单阈值（米）
    LARGE_ORDER_YARDS = 16.4  # 大订单阈值（码）= 15 / 0.9144

    def __init__(
        self,
        url: Optional[str] = None,
        db: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: bool = True,
        user_agent: Optional[str] = None,
    ):
        """Initialize Odoo client

        Args:
            url: Odoo instance URL (defaults to ODOO_URL env var)
            db: Database name (defaults to ODOO_DB env var)
            user: Username (defaults to ODOO_USER env var)
            password: Password (defaults to ODOO_PASSWORD env var)
            verify_ssl: Whether to verify SSL certificates
            user_agent: User-Agent header for all requests. Required for Odoo
                instances behind Cloudflare bot protection (error 1010), which
                blocks urllib's default "Python-urllib/x.y" signature.
                (defaults to ODOO_USER_AGENT env var, then DEFAULT_USER_AGENT)
        """
        # Normalize: strip a trailing /web — see normalize_odoo_base_url().
        self.url = normalize_odoo_base_url(url or os.environ.get("ODOO_URL", ""))
        self.db = db or os.environ.get("ODOO_DB", "")
        self.user = user or os.environ.get("ODOO_USER", "")
        self.password = password or os.environ.get("ODOO_PASSWORD", "")
        self.verify_ssl = verify_ssl
        self._user_agent = user_agent or os.environ.get(
            "ODOO_USER_AGENT", self.DEFAULT_USER_AGENT
        )

        # Session management
        self.cookie_jar = CookieJar()
        self._uid: Optional[int] = None

        # Setup SSL context
        if not verify_ssl:
            import ssl
            self._ssl_context = ssl.create_default_context()
            self._ssl_context.check_hostname = False
            self._ssl_context.verify_mode = ssl.CERT_NONE
            # Create opener with custom HTTPSHandler and cookie support
            self._opener = build_opener(
                HTTPCookieProcessor(self.cookie_jar),
                urllib.request.HTTPSHandler(context=self._ssl_context)
            )
        else:
            self._ssl_context = None
            # Create opener with cookie support (default SSL)
            self._opener = build_opener(HTTPCookieProcessor(self.cookie_jar))

        # Login if credentials provided
        if self.url and self.db and self.user and self.password:
            self.login()

    def _build_url(self, path: str) -> str:
        """Build full URL for API endpoint"""
        return f"{self.url.rstrip('/')}/{path.lstrip('/')}"

    def get_product_online_url(self, default_code: str, product_id: int) -> str:
        """Build product online shop URL

        Args:
            default_code: Product default code (e.g., "822917")
            product_id: Product template ID (e.g., 70800)

        Returns:
            Full online shop URL: {base_url}/shop/{default_code}-{product_id}
        """
        return f"{self.url.rstrip('/')}/shop/{default_code}-{product_id}"

    def _request(
        self,
        url: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Make HTTP request to Odoo

        Args:
            url: Full URL to request
            data: JSON-RPC payload

        Returns:
            Response data (result field from JSON-RPC response)

        Raises:
            urllib.error.HTTPError: On HTTP errors
            urllib.error.URLError: On connection errors
        """
        if data is None:
            data = {}

        payload = {
            "jsonrpc": "2.0",
            "method": data.get("method", "call"),
            "params": data.get("params", {}),
            "id": data.get("id", 1),
        }

        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": self._user_agent,
            },
        )

        # Handle SSL verification
        if not self.verify_ssl:
            import ssl

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx = None

        # Use opener with cookie support
        response = self._opener.open(req, timeout=120)
        response_data = json.loads(response.read().decode("utf-8"))

        if "error" in response_data:
            error = response_data["error"]
            raise Exception(f"Odoo Error: {error.get('data', {}).get('message', str(error))}")

        return response_data.get("result")

    def authenticate_with_detail(self, login: str, password: str) -> AuthResult:
        """Authenticate with Odoo and return detailed result.

        This method handles 2FA detection and different error types.
        It provides more detailed information than the basic login() method.

        Args:
            login: Odoo username/email
            password: Odoo password

        Returns:
            AuthResult with detailed status including:
            - success: Whether authentication succeeded
            - uid: User ID if successful
            - requires_2fa: Whether 2FA is required
            - error: Human-readable error message
            - error_type: Error category for programmatic handling
        """
        url = self._build_url("/web/session/authenticate")

        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "db": self.db,
                "login": login,
                "password": password,
            },
            "id": 1,
        }

        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": self._user_agent,
            },
        )

        try:
            # Use opener with cookie support
            response = self._opener.open(req, timeout=30)
            response_data = json.loads(response.read().decode("utf-8"))

            # Check for error in response
            if "error" in response_data:
                error_info = response_data["error"]
                error_message = (
                    error_info.get("data", {}).get("message")
                    or error_info.get("message", "Unknown error")
                )

                # Check if this is a 2FA error
                if self._is_2fa_error(error_message):
                    return AuthResult(
                        success=False,
                        requires_2fa=True,
                        error_type="2fa",
                        error="Two-factor authentication is enabled for your account. "
                        "Please temporarily disable 2FA in Odoo Settings or contact your administrator.",
                    )

                # Other credential errors
                return AuthResult(
                    success=False,
                    error_type="credentials",
                    error=error_message,
                )

            # Check result
            result = response_data.get("result", {})
            uid = result.get("uid")

            # Null uid typically means 2FA is required
            if uid is None:
                return AuthResult(
                    success=False,
                    requires_2fa=True,
                    error_type="2fa",
                    error="Two-factor authentication is enabled for your account. "
                    "Please temporarily disable 2FA in Odoo Settings or contact your administrator.",
                )

            # Success - store uid
            self._uid = uid

            return AuthResult(
                success=True,
                uid=uid,
            )

        except urllib.error.HTTPError as e:
            # Handle HTTP errors
            error_body = e.read().decode("utf-8") if e.fp else ""
            error_message = self._extract_error_message(error_body)

            if self._is_2fa_error(error_message):
                return AuthResult(
                    success=False,
                    requires_2fa=True,
                    error_type="2fa",
                    error="Two-factor authentication is enabled for your account.",
                )

            if e.code == 401:
                return AuthResult(
                    success=False,
                    error_type="credentials",
                    error="Invalid username or password.",
                )

            return AuthResult(
                success=False,
                error_type="network",
                error=f"HTTP Error {e.code}: {error_message}",
            )

        except urllib.error.URLError as e:
            # Handle SSL and network errors
            if isinstance(e.reason, ssl.SSLError):
                return AuthResult(
                    success=False,
                    error_type="ssl",
                    error="SSL certificate verification failed.",
                )

            if "timed out" in str(e.reason).lower() or "timeout" in str(e.reason).lower():
                return AuthResult(
                    success=False,
                    error_type="timeout",
                    error="Connection timed out. Please check your network.",
                )

            return AuthResult(
                success=False,
                error_type="network",
                error=f"Network error: {e.reason}",
            )

        except Exception as e:
            return AuthResult(
                success=False,
                error_type="unknown",
                error=f"Unexpected error: {str(e)}",
            )

    def _is_2fa_error(self, error_message: str) -> bool:
        """Check if error message indicates 2FA is required.

        Args:
            error_message: Error message to check

        Returns:
            True if error indicates 2FA is required
        """
        if not error_message:
            return False

        keywords = [
            "two-factor",
            "2fa",
            "otp",
            "verification",
            "verification code",
            "authenticator",
            "totp",
        ]

        error_lower = error_message.lower()
        return any(keyword in error_lower for keyword in keywords)

    def _extract_error_message(self, error_body: str) -> str:
        """Extract error message from error response body.

        Args:
            error_body: Raw error response body

        Returns:
            Extracted error message
        """
        try:
            data = json.loads(error_body)
            if "error" in data:
                error_info = data["error"]
                return (
                    error_info.get("data", {}).get("message")
                    or error_info.get("message", "")
                )
        except Exception:
            pass
        return error_body[:200] if error_body else "Unknown error"

    def verify_session(self) -> bool:
        """Verify if the current session is still valid on Odoo server.

        This method calls Odoo's /web/session/get_session_info endpoint to check if
        the stored session cookie is still valid on the server side.

        Returns:
            True if session is valid, False otherwise
        """
        url = self._build_url("/web/session/get_session_info")

        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {},
            "id": 1,
        }

        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": self._user_agent,
            },
        )

        try:
            response = self._opener.open(req, timeout=10)
            response_data = json.loads(response.read().decode("utf-8"))

            # Check for error in response
            if "error" in response_data:
                return False

            # Check if we got a valid user context with uid
            result = response_data.get("result", {})
            if result and result.get("uid"):
                return True

            return False

        except urllib.error.HTTPError:
            # 401, 403, 404 etc. indicate invalid session
            return False
        except urllib.error.URLError:
            # Network error - can't verify, assume invalid
            return False
        except Exception:
            # Any other error - assume invalid
            return False

    def login(self) -> int:
        """Authenticate with Odoo (backward compatible).

        This method maintains the original interface for backward compatibility.
        For detailed error handling, use authenticate_with_detail() instead.

        Returns:
            User ID (uid)

        Raises:
            Exception: On authentication failure
        """
        # First try internal user login
        return self._login_internal()

    def _login_internal(self) -> int:
        """Authenticate as internal user via /web/session/authenticate

        Returns:
            User ID (uid)

        Raises:
            Exception: On authentication failure
        """
        url = self._build_url("/web/session/authenticate")

        # Build JSON-RPC 2.0 request directly (matching Laravel OdooService)
        # Authentication endpoint expects params at root level, not wrapped in method
        payload = {
            "jsonrpc": "2.0",
            "params": {
                "db": self.db,
                "login": self.user,
                "password": self.password,
            },
        }

        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": self._user_agent,
            },
        )

        # Make request using main opener with cookie support
        response = self._opener.open(req, timeout=30)
        response_data = json.loads(response.read().decode("utf-8"))

        if "error" in response_data:
            error = response_data["error"]
            error_msg = error.get('data', {}).get('message', str(error))
            raise Exception(f"Authentication failed: {error_msg}")

        result = response_data.get("result", {})
        self._uid = result.get("uid")

        if self._uid is None:
            # Try alternative response format
            self._uid = response_data.get("result")

        return self._uid

    def _call(
        self,
        model: str,
        method: str,
        args: Optional[List] = None,
        kwargs: Optional[Dict] = None,
    ) -> Any:
        """Call Odoo model method

        Args:
            model: Model name (e.g., "sale.order", "product.product")
            method: Method name (e.g., "search_read", "write")
            args: Positional arguments
            kwargs: Keyword arguments

        Returns:
            Method result
        """
        if args is None:
            args = []
        if kwargs is None:
            kwargs = {}

        url = self._build_url("/web/dataset/call_kw")

        payload = {
            "method": "call",
            "params": {
                "model": model,
                "method": method,
                "args": args,
                "kwargs": kwargs,
            },
        }

        return self._request(url, payload)

    def execute_kw(
        self,
        model: str,
        method: str,
        args: list | None = None,
        kwargs: dict | None = None,
    ) -> Any:
        """Generic model-method call, mirroring Odoo's XML-RPC execute_kw.

        Public escape hatch for queries without a dedicated wrapper method
        (e.g. WhitelistService's res.partner / res.users loads). Same
        signature as xmlrpc ServerProxy.execute_kw: positional args list +
        kwargs dict.
        """
        return self._call(model, method, args, kwargs)

    # ==================== Customer Operations ====================

    def search_partner_by_code_or_name(self, keyword: str) -> Optional[Dict[str, Any]]:
        """Look up a partner by code (exact → fuzzy) then name (fuzzy).

        Returns the first match with id, name, code or None.
        """
        # Step 1: exact match on code
        result = self._call(
            model="res.partner",
            method="search_read",
            args=[[["code", "=", keyword]]],
            kwargs={"fields": ["id", "name", "code"], "limit": 1},
        )
        if result and isinstance(result, list) and len(result) > 0:
            return result[0]

        # Step 2: fuzzy match on code
        result = self._call(
            model="res.partner",
            method="search_read",
            args=[[["code", "ilike", keyword]]],
            kwargs={"fields": ["id", "name", "code"], "limit": 1},
        )
        if result and isinstance(result, list) and len(result) > 0:
            return result[0]

        # Step 3: fuzzy match on name
        result = self._call(
            model="res.partner",
            method="search_read",
            args=[[["name", "ilike", keyword]]],
            kwargs={"fields": ["id", "name", "code"], "limit": 1},
        )
        if result and isinstance(result, list) and len(result) > 0:
            return result[0]

        return None

    def search_customers_by_email(self, needle: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Look up CUSTOMER partners whose email field contains ``needle``.

        Matches the partner's own email OR any child contact's email
        (buyer inboxes often sit on the contact record under the company).
        ``customer_rank > 0`` + code set — the Gmail bind card offers these
        as click-to-bind candidates, so only code-bearing companies count.
        ``needle`` is matched case-insensitively as a substring (Odoo email
        fields may hold several ";"/","-separated addresses).
        """
        domain = [
            ("customer_rank", ">", 0),
            ("code", "!=", False),
            "|",
            ("email", "ilike", needle),
            ("child_ids.email", "ilike", needle),
        ]
        result = self._call(
            model="res.partner",
            method="search_read",
            args=[domain],
            kwargs={
                "fields": ["id", "name", "code", "email", "customer_rank"],
                "limit": limit,
                "order": "customer_rank desc, id asc",
            },
        )
        return result if isinstance(result, list) else []

    def search_customer(
        self,
        keyword: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search for customers by name

        Args:
            keyword: Search keyword
            limit: Maximum results

        Returns:
            List of customer records
        """
        domain = [["name", "ilike", keyword]]

        result = self._call(
            model="res.partner",
            method="search_read",
            args=[domain],
            kwargs={
                "fields": ["id", "name", "email", "street", "city", "x_studio_customer_remarks"],
                "limit": limit,
            },
        )

        return result if isinstance(result, list) else []

    def get_customer_addresses(
        self,
        partner_id: int,
    ) -> List[Dict[str, Any]]:
        """Get all addresses for a customer (child contacts)

        Args:
            partner_id: Customer ID

        Returns:
            List of address records
        """
        domain = [
            ["parent_id", "=", partner_id],
            ["type", "in", ["delivery", "invoice", "other"]],
        ]

        result = self._call(
            model="res.partner",
            method="search_read",
            args=[domain],
            kwargs={
                "fields": [
                    "id",
                    "name",
                    "street",
                    "street2",
                    "city",
                    "type",
                    "state_id",
                    "phone",
                    "mobile",
                ],
            },
        )

        return result if isinstance(result, list) else []

    def create_address(
        self,
        partner_id: int,
        name: str,
        street: str,
        street2: Optional[str] = None,
        city: Optional[str] = None,
        zip: Optional[str] = None,
        phone: Optional[str] = None,
        address_type: str = "delivery",
    ) -> int:
        """Create a new address for a customer

        Args:
            partner_id: Parent customer ID
            name: Address name
            street: Street address
            street2: Street address (line 2)
            city: City
            zip: Postal code
            phone: Contact phone number
            address_type: Address type (delivery/invoice/other)

        Returns:
            New address ID
        """
        data = {
            "parent_id": partner_id,
            "name": name,
            "street": street,
            "type": address_type,
        }

        if street2:
            data["street2"] = street2
        if city:
            data["city"] = city
        if zip:
            data["zip"] = zip
        if phone:
            data["phone"] = phone

        result = self._call(
            model="res.partner",
            method="create",
            args=[data],
        )

        return result

    # ==================== Product Operations ====================

    def search_product_by_code(
        self,
        code: str,
    ) -> Optional[Dict[str, Any]]:
        """Search for a product by default_code

        Note: Queries product.product (not product.template) because
        sale.order.line requires a product.product ID, not a template ID.

        Args:
            code: Product code

        Returns:
            Product record or None
        """
        domain = [["default_code", "=", code]]

        result = self._call(
            model="product.product",  # Changed: use product.product (variant) instead of product.template
            method="search_read",
            args=[domain],
            kwargs={
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "list_price",  # Sales price per meter
                    "secondary_uom_list_price",  # Price per yard
                    "description_sale",
                    "description",  # Product composition/description
                    "qty_available",
                    "virtual_available",
                    "stock_availability",  # oos / temp_out / '' / null
                    "temp_out_until",  # datetime or null
                    "allow_out_of_stock_order",  # boolean
                    "region_of_origin_id",  # Region of origin (many2one)
                    "average_weight_g",  # Average weight in grams
                    "width_cm",  # Width in centimeters
                    "product_yarn_id",  # Yarn count/fineness (many2one)
                    "brand_id",  # Brand (many2one)
                    "collection_name_full",  # Collection (char)
                    "sample_bookcat",  # Sample book
                    "equiv_super_lv",  # Yarn count
                    "product_tmpl_id",  # Product template ID for generating online URL
                    "shipping_days_min",  # Min shipping days
                ],
                "limit": 1,
            },
        )

        if result and isinstance(result, list) and len(result) > 0:
            return result[0]
        return None

    def search_product_by_code_fuzzy(
        self,
        code: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Fuzzy search on default_code when exact match fails.

        Uses a single ilike with leading/trailing wildcards.
        Returns minimal fields (id, default_code, name) for user selection.

        Args:
            code: The numeric code to search (already stripped of letters)
            limit: Max results (default 5)

        Returns:
            List of matching product records
        """
        domain = [["default_code", "ilike", f"%{code}%"]]
        result = self._call(
            model="product.product",
            method="search_read",
            args=[domain],
            kwargs={
                "fields": ["id", "default_code", "name"],
                "limit": limit,
                "order": "default_code ASC",
            },
        )
        return result if isinstance(result, list) else []

    @staticmethod
    def calculate_stock_status(product: dict) -> dict:
        """
        Calculate stock status display based on Odoo product data

        Priority order:
        1. stock_availability='oos' (註銷) → dropped, cannot order, no matter
           the stock quantity or the overselling flag
        2. allow_out_of_stock_order (overselling) → available
        3. stock quantity tiers (sold_out / low_stock / available)

        Args:
            product: Product data returned from Odoo

        Returns:
            Dictionary containing status information
        """
        allow_out_of_stock = product.get('allow_out_of_stock_order', False)
        qty_available = float(product.get('qty_available', 0))
        stock_availability = product.get('stock_availability', '')
        temp_out_until = product.get('temp_out_until')
        shipping_days_min = product.get('shipping_days_min')

        # Calculate shipping display (min ~ min+2 working days)
        shipping_display = None
        if shipping_days_min and shipping_days_min > 0:
            if shipping_days_min == 1:
                shipping_display = "1 working day"
            else:
                shipping_display = f"{shipping_days_min}-{shipping_days_min + 2} working days"

        # Level 0: Discontinued veto — stock_availability='oos' (註銷) means
        # the product is not sellable, regardless of leftover stock quantity
        # or the overselling flag.
        if stock_availability == 'oos':
            return {
                "status": "sold_out",
                "status_display": "Sold Out",
                "restock_status": "dropped",
                "restock_date_display": None,
                "restock_date": None,
                "can_order": False,
                "requires_confirmation": False,
                "confirmation_message": None,
                "guidance_message": "This product has been discontinued and cannot be ordered.",
                "shipping_days_min": shipping_days_min,
                "shipping_display": shipping_display,
            }

        # Level 1: Overselling mode
        if allow_out_of_stock:
            return {
                "status": "available",
                "status_display": "Available",
                "restock_status": None,
                "restock_date_display": None,
                "restock_date": None,
                "can_order": True,
                "requires_confirmation": False,
                "confirmation_message": None,
                "guidance_message": None,
                "shipping_days_min": shipping_days_min,
                "shipping_display": shipping_display,
            }

        # Level 2: Normal mode - Stock Status
        if qty_available <= 0:
            stock_status = "sold_out"
            stock_display = "Sold Out"
        elif qty_available < OdooClient.LOW_STOCK_THRESHOLD:
            stock_status = "low_stock"
            stock_display = "Low Stock"
        else:
            stock_status = "available"
            stock_display = "Available"

        # Restock Status / Date (only when qty <= RESTOCK_INFO_THRESHOLD)
        restock_status = None
        restock_date_display = None
        restock_date_obj = None

        # 'oos' never reaches here (handled by the Level 0 veto above)
        if (
            qty_available <= OdooClient.RESTOCK_INFO_THRESHOLD
            and stock_availability == 'temp_out'
            and temp_out_until
        ):
            try:
                if isinstance(temp_out_until, str):
                    # Handle ISO format string
                    restock_date_obj = datetime.fromisoformat(
                        temp_out_until.replace('Z', '+00:00')
                    )
                else:
                    restock_date_obj = temp_out_until

                # Check if in the future
                if restock_date_obj.tzinfo is None:
                    # No timezone info, compare with naive datetime
                    now = datetime.now()
                else:
                    now = datetime.now(restock_date_obj.tzinfo)

                if restock_date_obj > now:
                    restock_status = "restocking"
                    restock_date_display = (
                        f"Available from {restock_date_obj.strftime('%Y-%m-%d')}"
                    )
            except (ValueError, TypeError):
                # Date parsing failed, ignore
                pass

        # Build guidance_message ('dropped' is handled entirely by the
        # Level 0 veto, so only the temporary sold-out case remains here)
        guidance_message = None
        if stock_status == "sold_out":
            guidance_message = (
                "This product is temporarily out of stock and cannot be "
                "ordered (no backorders). You can cancel or browse other "
                "products in the same sample book."
            )

        # Build confirmation_message (NOTE: Do NOT include actual stock quantities)
        confirmation_message = None
        if stock_status == "low_stock":
            confirmation_message = (
                "[WARNING] Low stock. Continue with order?"
            )

        # Convert datetime to ISO string for JSON serialization
        restock_date_str = None
        if restock_date_obj is not None:
            restock_date_str = restock_date_obj.isoformat()

        return {
            "status": stock_status,
            "status_display": stock_display,
            "restock_status": restock_status,
            "restock_date_display": restock_date_display,
            "restock_date": restock_date_str,
            "can_order": stock_status != "sold_out",
            "requires_confirmation": stock_status == "low_stock",
            "confirmation_message": confirmation_message,
            "guidance_message": guidance_message,
            "shipping_days_min": shipping_days_min,
            "shipping_display": shipping_display,
        }

    @staticmethod
    def check_narrow_fabric_status(product: dict) -> dict:
        """Check if fabric is narrow (width < threshold).

        Args:
            product: Product data returned from Odoo

        Returns:
            Dictionary containing:
            {
                'is_narrow': bool,
                'width_cm': float|None,
                'warning_message': str|None
            }
        """
        width_cm = product.get('width_cm')

        if width_cm is None or width_cm <= 0 or width_cm >= OdooClient.NARROW_FABRIC_THRESHOLD_CM:
            return {
                'is_narrow': False,
                'width_cm': width_cm,
                'warning_message': None
            }

        return {
            'is_narrow': True,
            'width_cm': width_cm,
            'warning_message': (
                f"該面料寬度為{width_cm}cm，屬於窄幅面料，請確認所需碼數/米數是否足夠。"
            )
        }

    @staticmethod
    def check_width_mismatch(customer_width_cm, product: dict) -> dict:
        """Compare a customer-stated fabric width (門幅) with the system record.

        Args:
            customer_width_cm: width in cm as stated by the customer; a
                numeric string is accepted and coerced
            product: Product data returned from Odoo (needs 'width_cm')

        Returns:
            Dictionary containing:
            {
                'is_mismatch': bool,
                'customer_width_cm': float|None,
                'system_width_cm': float|None,
                'diff_cm': float|None,
                'warning_message': str|None
            }
        """
        try:
            customer_width = float(customer_width_cm)
        except (TypeError, ValueError):
            customer_width = None
        raw_system_width = product.get('width_cm')
        try:
            system_width = float(raw_system_width)
        except (TypeError, ValueError):
            system_width = None

        skip = {
            'is_mismatch': False,
            'customer_width_cm': customer_width,
            'system_width_cm': raw_system_width,
            'diff_cm': None,
            'warning_message': None
        }
        if (customer_width is None or customer_width <= 0
                or system_width is None or system_width <= 0):
            return skip

        diff_cm = round(abs(customer_width - system_width), 2)
        if diff_cm <= OdooClient.WIDTH_MISMATCH_TOLERANCE_CM:
            return skip

        return {
            'is_mismatch': True,
            'customer_width_cm': customer_width,
            'system_width_cm': system_width,
            'diff_cm': diff_cm,
            'warning_message': (
                f"客戶提供門幅 {customer_width:g}cm，此款門幅為 "
                f"{system_width:g}cm（相差 {diff_cm:g}cm），"
                "已向客戶提示門幅差異。"
            )
        }

    @staticmethod
    def check_large_order_status(requested_qty: float, unit: str) -> dict:
        """Check if the requested quantity is a large order.

        Large orders (15M+) require special handling - customers should be
        guided to contact customer service for special pricing and availability.

        Args:
            requested_qty: Requested quantity value
            unit: 'm' for meters, 'y' for yards

        Returns:
            Dictionary containing:
            {
                'is_large_order': bool,
                'threshold_meters': float,
                'threshold_yards': float,
                'guidance_message': str|None
            }
        """
        # Convert to meters for comparison
        if unit == 'm':
            qty_meters = requested_qty
        else:
            qty_meters = requested_qty * 0.9144  # yards to meters

        is_large = qty_meters >= OdooClient.LARGE_ORDER_METERS

        return {
            'is_large_order': is_large,
            'threshold_meters': OdooClient.LARGE_ORDER_METERS,
            'threshold_yards': OdooClient.LARGE_ORDER_YARDS,
            'guidance_message': (
                "For orders of 15 meters or more, please contact our customer service "
                "for special pricing and availability confirmation."
            ) if is_large else None
        }

    def search_product_by_name(
        self,
        name: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search for products by name

        Args:
            name: Search keyword
            limit: Maximum results

        Returns:
            List of product records
        """
        domain = [["name", "ilike", name]]

        result = self._call(
            model="product.template",
            method="search_read",
            args=[domain],
            kwargs={
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "list_price",  # Sales price per meter
                    "secondary_uom_list_price",  # Price per yard
                    "description_sale",
                    "description",  # Product composition/description
                    "qty_available",
                    "virtual_available",
                    "stock_availability",  # oos / temp_out / '' / null
                    "temp_out_until",  # datetime or null
                    "allow_out_of_stock_order",  # boolean
                    "region_of_origin_id",  # Region of origin (many2one)
                    "average_weight_g",  # Average weight in grams
                    "width_cm",  # Width in centimeters
                    "brand_id",  # Brand (many2one)
                    "collection_name_full",  # Collection (char)
                    "sample_bookcat",  # Sample book category
                    "equiv_super_lv",  # Yarn count
                    "shipping_days_min",  # Min shipping days
                ],
                "limit": limit,
            },
        )

        return result if isinstance(result, list) else []

    def search_products_with_stock(
        self,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Search for products with available stock

        Args:
            limit: Maximum results

        Returns:
            List of product records
        """
        domain = [["qty_available", ">", 0]]

        result = self._call(
            model="product.template",
            method="search_read",
            args=[domain],
            kwargs={
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "list_price",  # Sales price per meter
                    "secondary_uom_list_price",  # Price per yard
                    "description_sale",
                    "description",  # Product composition/description
                    "qty_available",
                    "virtual_available",
                    "stock_availability",  # oos / temp_out / '' / null
                    "temp_out_until",  # datetime or null
                    "allow_out_of_stock_order",  # boolean
                    "region_of_origin_id",  # Region of origin (many2one)
                    "average_weight_g",  # Average weight in grams
                    "width_cm",  # Width in centimeters
                    "product_yarn_id",  # Yarn count/fineness (many2one)
                    "brand_id",  # Brand (many2one)
                    "collection_name_full",  # Collection (char)
                    "sample_bookcat",  # Sample book (样布册)
                    "equiv_super_lv",  # Yarn count (纱支数)
                    "shipping_days_min",  # Min shipping days
                ],
                "limit": limit,
                "order": "qty_available DESC",
            },
        )

        return result if isinstance(result, list) else []

    def get_public_category_by_name_prefix(
        self,
        prefix: str,
        limit: int = 1,
    ) -> Optional[Dict[str, Any]]:
        """Search for public category by name prefix

        Args:
            prefix: Category name prefix (e.g., "6606")
            limit: Maximum results

        Returns:
            Category record or None
        """
        domain = [["name", "=like", f"{prefix}%"]]

        result = self._call(
            model="product.public.category",
            method="search_read",
            args=[domain],
            kwargs={
                "fields": ["id", "name"],
                "limit": limit,
            },
        )

        if result and isinstance(result, list) and len(result) > 0:
            return result[0]
        return None

    def get_products_by_sample_book_number(
        self,
        book_number: str,
        only_in_stock: bool = True,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get products by sample book number using default_code prefix

        Sample book products are named with the 4-digit book number as prefix
        followed by 2 digits (e.g., "227707", "227701" for book 2277).

        Note: Stock filtering is done in Python to avoid slow Odoo queries
        with computed fields.

        Args:
            book_number: Sample book number (e.g., "6606")
            only_in_stock: Only return sellable products — real stock above
                the residue epsilon and not discontinued (stock_availability
                != 'oos')
            limit: Maximum results

        Returns:
            List of product records
        """
        # Search for product IDs by default_code prefix (without stock filter)
        # Product codes are 6 digits: book_number (4) + sequence (2)
        domain = [["default_code", "=like", f"{book_number}%"]]

        # Get more IDs to account for stock filtering
        search_limit = limit * 5 if only_in_stock else limit

        product_ids = self._call(
            model="product.template",
            method="search",
            args=[domain],
            kwargs={
                "limit": search_limit,
                "order": "default_code ASC",
            },
        )

        if not product_ids:
            return []

        # Read product data in batches
        batch_size = 50
        all_products = []

        for i in range(0, min(len(product_ids), search_limit), batch_size):
            batch_ids = product_ids[i:i + batch_size]

            batch = self._call(
                model="product.template",
                method="read",
                args=[batch_ids],
                kwargs={
                    "fields": [
                        "id",
                        "name",
                        "default_code",
                        "list_price",
                        "secondary_uom_list_price",
                        "description_sale",
                        "description",
                        "qty_available",
                        "virtual_available",
                        "stock_availability",  # oos = 註銷，不可推荐
                        "region_of_origin_id",
                        "average_weight_g",
                        "width_cm",
                        "product_yarn_id",  # Yarn count/fineness (many2one)
                        "brand_id",  # Brand (many2one)
                        "collection_name_full",  # Collection (char)
                    ],
                },
            )

            all_products.extend(batch)

        # Filter by stock in Python if requested: exclude discontinued (oos)
        # products no matter their leftover quantity, and treat measurement
        # residue (below STOCK_RESIDUE_EPSILON yards) as no stock.
        if only_in_stock:
            all_products = [
                p for p in all_products
                if p.get("qty_available", 0) > OdooClient.STOCK_RESIDUE_EPSILON
                and p.get("stock_availability") != "oos"
            ]

        # Apply limit
        return all_products[:limit]

    # ==================== Brand Operations ====================

    # Brand & Collection root category ID (fixed in Odoo)
    BRAND_COLLECTION_ROOT_ID = 1

    def get_all_brand_categories(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get all brand categories (direct children of Brand & Collection).

        Args:
            limit: Maximum number of brands to return

        Returns:
            List of brand category records with id, name, parent_id
        """
        result = self._call(
            model="product.public.category",
            method="search_read",
            args=[[["parent_id", "=", self.BRAND_COLLECTION_ROOT_ID]]],
            kwargs={
                "fields": ["id", "name", "parent_id"],
                "limit": limit,
                "order": "name ASC",
            },
        )
        return result if isinstance(result, list) else []

    def search_brand_category_by_name(
        self,
        name: str,
        exact: bool = False,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Search for brand categories by name (fuzzy match by default).

        Searches in product.public.category model, limited to children of Brand & Collection.

        Args:
            name: Brand name to search for
            exact: If True, use exact match; if False, use case-insensitive partial match
            limit: Maximum number of results

        Returns:
            List of matching brand category records with id, name
        """
        domain = [
            ["parent_id", "=", self.BRAND_COLLECTION_ROOT_ID],
            ["name", "ilike" if not exact else "=", name]
        ]

        result = self._call(
            model="product.public.category",
            method="search_read",
            args=[domain],
            kwargs={
                "fields": ["id", "name"],
                "limit": limit,
                "order": "name ASC",
            },
        )
        return result if isinstance(result, list) else []

    def search_collection_by_name(
        self,
        name: str,
        exact: bool = False,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Search for collections/series by name (fuzzy match by default).

        Searches in product.public.category model for all categories under Brand & Collection
        (including brands, collections, and sample books).

        Args:
            name: Collection/series name to search for (e.g., "8239", "Fantasia")
            exact: If True, use exact match; if False, use case-insensitive partial match
            limit: Maximum number of results

        Returns:
            List of matching category records with id, name, parent_id
        """
        # Search for categories that are descendants of Brand & Collection (id=1)
        # Use child_of operator to get all descendants
        domain = [
            ["id", "child_of", self.BRAND_COLLECTION_ROOT_ID],
            ["id", "!=", self.BRAND_COLLECTION_ROOT_ID],  # Exclude root itself
            ["name", "ilike" if not exact else "=", name]
        ]

        result = self._call(
            model="product.public.category",
            method="search_read",
            args=[domain],
            kwargs={
                "fields": ["id", "name", "parent_id"],
                "limit": limit,
                "order": "name ASC",
            },
        )
        return result if isinstance(result, list) else []

    def get_products_by_brand(
        self,
        brand_id: int,
        only_in_stock: bool = False,
        limit: int = 30
    ) -> List[Dict[str, Any]]:
        """Get products by brand ID.

        Args:
            brand_id: Brand ID from product.brand model
            only_in_stock: Only return products with available stock
            limit: Maximum number of products to return

        Returns:
            List of product records
        """
        domain = [["brand_id", "=", brand_id]]
        if only_in_stock:
            domain.append(["qty_available", ">", 0])

        result = self._call(
            model="product.product",
            method="search_read",
            args=[domain],
            kwargs={
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "list_price",
                    "secondary_uom_list_price",
                    "qty_available",
                    "width_cm",
                    "brand_id",
                    "collection_name_full",
                ],
                "limit": limit,
                "order": "default_code ASC",
            },
        )
        return result if isinstance(result, list) else []

    # ==================== UOM (Unit of Measure) ====================

    def get_uom_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Get UOM by name

        Args:
            name: UOM name (e.g., "Meter", "Yard", "米", "码")

        Returns:
            UOM record or None
        """
        domain = [["name", "ilike", name]]

        result = self._call(
            model="uom.uom",
            method="search_read",
            args=[domain],
            kwargs={
                "fields": ["id", "name"],
                "limit": 1,
            },
        )

        if result and isinstance(result, list) and len(result) > 0:
            return result[0]
        return None

    # ==================== Order Operations ====================

    def create_order(
        self,
        partner_id: int,
        note: str = "",
        partner_shipping_id: Optional[int] = None,
        partner_invoice_id: Optional[int] = None,
        company_id: Optional[int] = None,
    ) -> int:
        """Create a new sale order

        Args:
            partner_id: Customer ID
            note: Order note
            partner_shipping_id: Delivery address ID (optional)
            partner_invoice_id: Invoice address ID (optional)
            company_id: Target company ID (optional). When set, the order is
                pinned to this Odoo company regardless of the authenticated
                user's default company. When omitted, Odoo falls back to the
                user's current company.

        Returns:
            New order ID
        """
        data = {
            "partner_id": partner_id,
        }

        if note:
            data["note"] = note
        if partner_shipping_id:
            data["partner_shipping_id"] = partner_shipping_id
        if partner_invoice_id:
            data["partner_invoice_id"] = partner_invoice_id
        if company_id:
            data["company_id"] = company_id

        result = self._call(
            model="sale.order",
            method="create",
            args=[data],
        )

        return result

    def get_order(
        self,
        order_id: int,
    ) -> Optional[Dict[str, Any]]:
        """Get order details

        Args:
            order_id: Order ID

        Returns:
            Order record or None
        """
        result = self._call(
            model="sale.order",
            method="read",
            args=[[order_id]],
            kwargs={
                "fields": [
                    "id",
                    "name",
                    "state",
                    "partner_id",
                    "partner_shipping_id",
                    "partner_invoice_id",
                    "client_order_ref",
                    "payment_term_id",
                    "amount_total",
                    "amount_untaxed",
                    "order_line",
                    "date_order",
                    "access_token",
                ],
            },
        )

        if result and isinstance(result, list) and len(result) > 0:
            return result[0]
        return None

    def search_orders(
        self,
        partner_id: Optional[int] = None,
        state: Optional[str] = None,
        days: Optional[int] = None,
        limit: int = 10,
        exclude_states: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Search for sale orders

        Args:
            partner_id: Filter by customer
            state: Filter by state (draft/sent/sale/done/cancel)
            days: Filter orders within last N days (default: no filter)
            limit: Maximum results
            exclude_states: Order states to exclude (e.g. ["cancel"]). Useful
                for candidate-selection lists that should not surface orders
                the customer can no longer act on.

        Returns:
            List of order records
        """
        domain = []

        if partner_id:
            domain.append(["partner_id", "=", partner_id])
        if state:
            domain.append(["state", "=", state])
        if exclude_states:
            domain.append(["state", "not in", exclude_states])
        if days is not None:
            from datetime import datetime, timedelta, timezone

            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            domain.append(["date_order", ">=", cutoff.strftime("%Y-%m-%d %H:%M:%S")])

        result = self._call(
            model="sale.order",
            method="search_read",
            args=[domain] if domain else [[]],
            kwargs={
                "fields": [
                    "id",
                    "name",
                    "state",
                    "partner_id",
                    "amount_total",
                    "currency_id",
                    "date_order",
                ],
                "limit": limit,
                "order": "date_order DESC",
            },
        )

        return result if isinstance(result, list) else []

    def add_order_line(
        self,
        order_id: int,
        product_id: int,
        qty: float,
        price: float,
        uom_id: Optional[int] = None,
        customer_po: Optional[str] = None,
    ) -> int:
        """Add a line to a sale order

        Args:
            order_id: Order ID
            product_id: Product ID
            qty: Quantity
            price: Unit price (must match the UOM: price per meter if uom_id is meter, etc.)
            uom_id: Optional UOM ID (if not specified, uses product's default UOM)
            customer_po: Optional Customer PO Number

        Returns:
            New order line ID
        """
        data = {
            "order_id": order_id,
            "product_id": product_id,
            "product_uom_qty": qty,
            "price_unit": price,
        }

        # Set UOM if specified
        if uom_id is not None:
            data["product_uom"] = uom_id

        # Set customer_po if provided
        if customer_po:
            data["customer_po"] = customer_po

        logger.debug(f"Creating order line with data: {data}")
        result = self._call(
            model="sale.order.line",
            method="create",
            args=[data],
        )
        logger.info(f"Order line created successfully, line_id={result}")

        return result

    def update_order(
        self,
        order_id: int,
        **kwargs,
    ) -> bool:
        """Update a sale order with verification

        Args:
            order_id: Order ID
            **kwargs: Fields to update (e.g., partner_shipping_id, payment_term_id)

        Returns:
            True if successfully updated and verified, False otherwise
        """
        import logging
        logger = logging.getLogger(__name__)

        if not kwargs:
            return True

        # Get field names to verify (handle many2one fields which return [id, name])
        fields_to_read = []
        for key in kwargs.keys():
            fields_to_read.append(key)

        # Read current values before update
        try:
            current = self._call(
                model="sale.order",
                method="read",
                args=[[order_id]],
                kwargs={"fields": fields_to_read},
            )
        except Exception as e:
            logger.error(f"[update_order] Failed to read order {order_id} before update: {e}")
            return False

        if not current or not isinstance(current, list) or len(current) == 0:
            logger.error(f"[update_order] Order {order_id} not found")
            return False

        # Perform the update
        try:
            self._call(
                model="sale.order",
                method="write",
                args=[[order_id], kwargs],
            )
        except Exception as e:
            logger.error(f"[update_order] Failed to update order {order_id}: {e}")
            return False

        # Verify the update was successful
        try:
            updated = self._call(
                model="sale.order",
                method="read",
                args=[[order_id]],
                kwargs={"fields": fields_to_read},
            )
        except Exception as e:
            logger.error(f"[update_order] Failed to read order {order_id} after update: {e}")
            return False

        if not updated or not isinstance(updated, list) or len(updated) == 0:
            logger.error(f"[update_order] Order {order_id} not found after update")
            return False

        updated_order = updated[0]

        # Check each field was actually updated
        for key, expected_value in kwargs.items():
            actual_value = updated_order.get(key)

            # Handle many2one fields which return [id, name]
            if isinstance(actual_value, list):
                actual_id = actual_value[0] if len(actual_value) > 0 else None
                if actual_id != expected_value:
                    logger.warning(
                        f"[update_order] Field '{key}' not updated correctly: "
                        f"expected ID {expected_value}, got {actual_value}"
                    )
                    return False
            else:
                if actual_value != expected_value:
                    logger.warning(
                        f"[update_order] Field '{key}' not updated correctly: "
                        f"expected {expected_value}, got {actual_value}"
                    )
                    return False

        logger.info(f"[update_order] Order {order_id} updated successfully")
        return True

    def confirm_order(
        self,
        order_id: int,
    ) -> bool:
        """Confirm a sale order

        Args:
            order_id: Order ID

        Returns:
            True if successful
        """
        self._call(
            model="sale.order",
            method="action_confirm",
            args=[[order_id]],
        )
        return True

    def send_quotation(
        self,
        order_id: int,
    ) -> bool:
        """Mark a draft quotation as 'sent' (Quotation Sent).

        This is the draft → sent transition, equivalent to a monthly-billing
        customer placing an order online. Distinct from confirm_order() which
        calls action_confirm (→ 'sale').

        Writes state='sent' directly and verifies the result. This is
        intentionally distinct from action_confirm (→ 'sale') and from
        action_quotation_send (which opens an email wizard).

        Args:
            order_id: Order ID

        Returns:
            True if successful
        """
        order = self.get_order(order_id)
        if not order:
            raise Exception(f"Order {order_id} not found")

        state = order.get("state", "")
        if state == "sent":
            return True
        if state != "draft":
            raise Exception(
                f"Cannot send quotation for order {order_id} from state '{state}'"
            )

        self._call(
            model="sale.order",
            method="write",
            args=[[order_id], {"state": "sent"}],
        )

        updated = self.get_order(order_id)
        if not updated or updated.get("state") != "sent":
            new_state = updated.get("state") if updated else None
            raise Exception(
                f"Failed to mark order {order_id} as sent; current state is '{new_state}'"
            )
        return True

    def cancel_order(
        self,
        order_id: int,
    ) -> bool:
        """Cancel a sale order

        For confirmed orders (state='sale'), need to unlock first before cancelling.

        Args:
            order_id: Order ID

        Returns:
            True if successful
        """
        # Get current order state
        order = self.get_order(order_id)
        if not order:
            raise Exception(f"Order {order_id} not found")

        state = order.get("state", "")

        # If order is confirmed (sale), need to unlock it first
        if state == "sale":
            self._call(
                model="sale.order",
                method="action_draft",
                args=[[order_id]],
            )

        # Now cancel the order
        self._call(
            model="sale.order",
            method="action_cancel",
            args=[[order_id]],
        )
        return True

    def remove_order_line(
        self,
        line_id: int,
    ) -> bool:
        """Remove a line from a sale order

        Args:
            line_id: Order line ID

        Returns:
            True if successful
        """
        self._call(
            model="sale.order.line",
            method="unlink",
            args=[[line_id]],
        )
        return True

    def update_order_line(
        self,
        line_id: int,
        qty: Optional[float] = None,
        price: Optional[float] = None,
        customer_po: Optional[str] = None,
    ) -> bool:
        """Update a line in a sale order

        Args:
            line_id: Order line ID
            qty: New quantity (optional)
            price: New unit price (optional)
            customer_po: Customer PO Number (optional)

        Returns:
            True if successful
        """
        data = {}
        if qty is not None:
            data["product_uom_qty"] = qty
        if price is not None:
            data["price_unit"] = price
        if customer_po is not None:
            data["customer_po"] = customer_po

        if not data:
            raise ValueError("At least one of qty, price, or customer_po must be provided")

        self._call(
            model="sale.order.line",
            method="write",
            args=[[line_id], data],
        )
        return True

    def get_order_lines(self, order_id: int) -> List[Dict[str, Any]]:
        """Get all order lines for an order

        Args:
            order_id: Order ID

        Returns:
            List of order line records with full details
        """
        # First get the order to find line IDs
        order = self.get_order(order_id)
        if not order:
            return []

        line_ids = order.get("order_line", [])
        if not line_ids:
            return []

        # Read all line details
        lines = self._call(
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
                ],
            },
        )

        return lines if isinstance(lines, list) else []

    def search_order_lines(
        self,
        partner_id: int,
        product_code: Optional[str] = None,
        days: int = 180,
        exclude_states: Optional[List[str]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Search sale order lines across all orders for a customer.

        Uses Odoo dot-notation domain to filter sale.order.line by parent
        sale.order fields (partner_id, date_order, state) in a single query.

        Args:
            partner_id: Customer partner ID to filter by
            product_code: Optional product default_code filter (e.g., "227707")
            days: Time range in days (default 180)
            exclude_states: Order states to exclude (default: ["cancel", "draft"]
                — history means orders the customer actually placed, so
                unconfirmed draft quotations don't count)
            limit: Maximum results (default 100)

        Returns:
            List of order line records
        """
        from datetime import timedelta

        if exclude_states is None:
            exclude_states = ["cancel", "draft"]

        domain = [
            ["order_id.partner_id", "=", partner_id],
            [
                "order_id.date_order",
                ">=",
                (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
                    "%Y-%m-%d"
                ),
            ],
            ["order_id.state", "not in", exclude_states],
        ]

        if product_code:
            domain.append(["product_default_code", "=", product_code])

        result = self._call(
            model="sale.order.line",
            method="search_read",
            args=[domain],
            kwargs={
                "fields": [
                    "id",
                    "order_id",
                    "product_id",
                    "product_default_code",
                    "name",
                    "product_uom_qty",
                    "product_uom",
                    "price_unit",
                    "discount",
                    "price_subtotal",
                    "create_date",
                ],
                "limit": limit,
                "order": "create_date DESC",
            },
        )

        return result if isinstance(result, list) else []

    def update_order_note(self, order_id: int, note: str) -> bool:
        """Update order note/remark

        Args:
            order_id: Order ID
            note: New note content

        Returns:
            True if successful
        """
        self._call(
            model="sale.order",
            method="write",
            args=[[order_id], {"note": note}],
        )
        return True

    def write_order_remarks(
        self,
        order_id: int,
        so_remarks: Optional[str] = None,
        customer_remarks: Optional[str] = None,
    ) -> bool:
        """Write remarks to sale.order level.

        Writes to x_studio_so_remarks (SO Remarks, char field) and/or
        x_studio_customer_remarks (Customer Remarks, text field).

        Args:
            order_id: Sale order ID
            so_remarks: Value for x_studio_so_remarks (char, max 255)
            customer_remarks: Value for x_studio_customer_remarks (text)

        Returns:
            True if successful
        """
        vals = {}
        if so_remarks is not None:
            vals["x_studio_so_remarks"] = so_remarks
        if customer_remarks is not None:
            vals["x_studio_customer_remarks"] = customer_remarks
        if vals:
            self._call("sale.order", "write", [[order_id], vals])
        return True

    def write_order_line_remarks(
        self,
        line_id: int,
        remarks: Optional[str] = None,
    ) -> bool:
        """Write remarks to sale.order.line level.

        Args:
            line_id: Sale order line ID
            remarks: Value for remarks field (char)

        Returns:
            True if successful
        """
        vals = {}
        if remarks is not None:
            vals["remarks"] = remarks
        if vals:
            self._call("sale.order.line", "write", [[line_id], vals])
        return True

    def get_order_remarks(self, order_id: int) -> Dict[str, Any]:
        """Read current remarks from a sale.order.

        Returns dict with x_studio_so_remarks, x_studio_customer_remarks, note,
        state, and partner_id.
        Gracefully handles missing fields (returns None for them).

        Args:
            order_id: Sale order ID

        Returns:
            Dict of field values
        """
        try:
            fields = [
                "x_studio_so_remarks",
                "x_studio_customer_remarks",
                "note",
                "state",
                "partner_id",
            ]
            result = self._call(
                "sale.order", "read",
                [[order_id]],
                {"fields": fields},
            )
            if result and isinstance(result, list) and len(result) > 0:
                return result[0]
        except Exception:
            pass
        return {}

    def get_order_line_remarks(self, line_id: int) -> Dict[str, Any]:
        """Read current remarks from a sale.order.line.

        Returns dict with remarks field value.
        Gracefully handles missing fields.

        Args:
            line_id: Sale order line ID

        Returns:
            Dict with remarks and order_id
        """
        try:
            result = self._call(
                "sale.order.line", "read",
                [[line_id]],
                {"fields": ["remarks", "order_id"]},
            )
            if result and isinstance(result, list) and len(result) > 0:
                return result[0]
        except Exception:
            pass
        return {}

    def check_field_exists(self, model: str, field_name: str) -> bool:
        """Check if a field exists on an Odoo model.

        Args:
            model: Odoo model name (e.g. "sale.order")
            field_name: Field name to check

        Returns:
            True if field exists, False otherwise
        """
        try:
            fields_info = self._call(
                model, "fields_get",
                [[field_name]],
                {"attributes": ["type", "size"]},
            )
            return field_name in fields_info
        except Exception:
            return False

    def get_field_type(self, model: str, field_name: str) -> Optional[Dict[str, Any]]:
        """Get field metadata (type, size) from Odoo.

        Args:
            model: Odoo model name
            field_name: Field name

        Returns:
            Dict with 'type' and 'size' keys, or None if field not found
        """
        try:
            fields_info = self._call(
                model, "fields_get",
                [[field_name]],
                {"attributes": ["type", "size"]},
            )
            if field_name in fields_info:
                return fields_info[field_name]
        except Exception:
            pass
        return None

    # ==================== Dye Lot / Batch ====================

    def get_sale_order_line_lot_fields(self) -> List[str]:
        """Detect any x_studio_*lot* custom fields on sale.order.line.

        Returns list of matching field names, or empty list if none found.
        """
        try:
            all_fields = self._call(
                model="sale.order.line",
                method="fields_get",
                args=[[]],
            )
            if not all_fields:
                return []
            import re as _re
            return [
                f for f in all_fields
                if f.startswith("x_studio") and _re.search(r"lot", f, _re.IGNORECASE)
            ]
        except Exception:
            return []

    def get_picking_lot_info(
        self,
        order_id: int,
        product_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Get lot info from picking chain for a sale order.

        Chain: sale.order → picking_ids → move_line_ids → lot_id
        Only works for confirmed orders with generated pickings.

        Args:
            order_id: Sale order ID
            product_id: Optional product.product ID to filter

        Returns:
            List of dicts with lot info: {lot_name, product_code, qty_done}
        """
        try:
            # Get pickings for this order
            pickings = self._call(
                model="stock.picking",
                method="search_read",
                args=[["sale_id", "=", order_id]],
                kwargs={"fields": ["id"]},
            )
            if not pickings:
                return []

            picking_ids = [p["id"] for p in pickings]

            # Get stock.move for these pickings (filtered by product if specified)
            move_domain = [("picking_id", "in", picking_ids)]
            if product_id:
                move_domain.append(("product_id", "=", product_id))

            moves = self._call(
                model="stock.move",
                method="search_read",
                args=[move_domain],
                kwargs={"fields": ["id"], "limit": 50},
            )
            if not moves:
                return []

            move_ids = [m["id"] for m in moves]

            # Get move lines with lot info
            line_domain = [("move_id", "in", move_ids)]
            if product_id:
                line_domain.append(("product_id", "=", product_id))

            move_lines = self._call(
                model="stock.move.line",
                method="search_read",
                args=[line_domain],
                kwargs={
                    "fields": ["lot_id", "product_id", "qty_done"],
                    "limit": 100,
                },
            )

            results = []
            for ml in move_lines:
                lot = ml.get("lot_id")
                if lot and isinstance(lot, list) and len(lot) > 1:
                    results.append({
                        "lot_name": lot[1],
                        "product_id": ml.get("product_id", [None])[0] if isinstance(ml.get("product_id"), list) else ml.get("product_id"),
                        "qty_done": ml.get("qty_done", 0),
                    })
            return results
        except Exception:
            return []

    # ==================== Payment Terms ====================

    def get_payment_terms(self) -> List[Dict[str, Any]]:
        """Get all payment terms

        Returns:
            List of payment term records
        """
        result = self._call(
            model="account.payment.term",
            method="search_read",
            args=[[]],
            kwargs={
                "fields": ["id", "name"],
                "order": "name ASC",
            },
        )

        return result if isinstance(result, list) else []

    def get_partner_payment_profile(self, partner_id: int) -> Optional[Dict[str, Any]]:
        """Read a partner's payment profile for the balance check (read-only).

        Combines the payment term (with credit_limit_jsi's ``is_cod`` flag)
        and the computed balance fields the 导航面板 balance review uses:
        ``total_due`` (Due Amount — negative = customer's remaining deposit),
        ``remaining_credit`` (Remaining Credits — already nets 订料单) and
        ``total_pending_sales_credit`` (订料单: confirmed, uninvoiced orders).

        Returns:
            {partner_id, term_id, term_name, term_is_cod, total_due,
             remaining_credit, pending_sales} or None if partner not found.
            ``term_is_cod`` is None when the flag is unavailable (e.g. Odoo
            without credit_limit_jsi).
        """
        rows = self._call(
            model="res.partner",
            method="search_read",
            args=[[["id", "=", partner_id]]],
            kwargs={
                "fields": [
                    "id",
                    "property_payment_term_id",
                    "total_due",
                    "remaining_credit",
                    "total_pending_sales_credit",
                ],
                "limit": 1,
            },
        )
        if not rows or not isinstance(rows, list):
            return None
        row = rows[0]

        term = row.get("property_payment_term_id")
        if isinstance(term, (list, tuple)) and term:
            term_id, term_name = int(term[0]), term[1]
        elif term:
            term_id, term_name = int(term), ""
        else:
            term_id, term_name = None, ""

        term_is_cod: Optional[bool] = None
        if term_id:
            try:
                terms = self._call(
                    model="account.payment.term",
                    method="read",
                    args=[[term_id]],
                    kwargs={"fields": ["id", "name", "is_cod"]},
                )
                if terms and isinstance(terms, list):
                    term_is_cod = bool(terms[0].get("is_cod"))
            except Exception as e:
                logger.warning(
                    f"[get_partner_payment_profile] is_cod read failed for "
                    f"term {term_id}: {e}"
                )

        return {
            "partner_id": row["id"],
            "term_id": term_id,
            "term_name": term_name,
            "term_is_cod": term_is_cod,
            "total_due": float(row.get("total_due") or 0.0),
            "remaining_credit": float(row.get("remaining_credit") or 0.0),
            "pending_sales": float(row.get("total_pending_sales_credit") or 0.0),
        }

    # ==================== Portal URL ====================

    def get_order_portal_url(
        self,
        order_id: int,
    ) -> Optional[str]:
        """Get the portal URL for an order.

        Uses Odoo's get_portal_url() which auto-generates an access_token
        if one doesn't exist yet.

        Args:
            order_id: Order ID

        Returns:
            Full portal URL (e.g. https://domain/my/orders/123?access_token=xxx)
            or None if the order doesn't exist.
        """
        try:
            path = self._call("sale.order", "get_portal_url", [[order_id]])
            if path:
                base_url = self.url.rstrip("/")
                return f"{base_url}{path}"
        except Exception as e:
            logger.warning(f"Failed to get portal URL for order {order_id}: {e}")
        return None

    # ==================== User Profile ====================

    def get_user_profile(self, uid: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Fetch user profile from Odoo.

        Gets comprehensive user information including name, email, company,
        and user type (internal/portal).

        Args:
            uid: User ID (uses logged-in user's ID if not provided)

        Returns:
            Dictionary with user profile data or None if fetch fails:
            {
                'odoo_uid': int,
                'display_name': str,
                'email': str,
                'company_name': Optional[str],
                'company_id': Optional[int],
                'user_type': str,  # 'internal' or 'portal'
                'language': str,
                'partner_id': int,
                'is_company': bool
            }
        """
        uid = uid or self._uid
        if not uid:
            return None

        try:
            # Get user data
            users = self._call(
                model="res.users",
                method="read",
                args=[[uid]],
                kwargs={
                    "fields": [
                        "name",
                        "email",
                        "login",
                        "partner_id",
                        "company_id",
                        "lang",
                        "share",  # True for portal users
                    ]
                },
            )

            if not users or not isinstance(users, list):
                return None

            user = users[0]

            # Get partner_id
            partner_id = user.get("partner_id")
            if partner_id and isinstance(partner_id, (list, tuple)):
                partner_id = partner_id[0]

            # Determine user type
            user_type = "portal" if user.get("share") else "internal"

            # Get partner data for company info
            company_name = None
            company_id = None
            is_company = False

            if partner_id:
                partners = self._call(
                    model="res.partner",
                    method="read",
                    args=[[partner_id]],
                    kwargs={
                        "fields": [
                            "name",
                            "email",
                            "is_company",
                            "parent_id",
                            "company_id",
                        ]
                    },
                )

                if partners and isinstance(partners, list):
                    partner = partners[0]
                    is_company = partner.get("is_company", False)

                    # Priority order for company name:
                    # 1. If partner is a company, use its name
                    if is_company:
                        company_name = partner.get("name")
                        company_id = partner_id
                    # 2. If partner has a parent_id, use parent's name
                    elif partner.get("parent_id"):
                        parent_id = partner["parent_id"]
                        if isinstance(parent_id, (list, tuple)):
                            parent_id = parent_id[0]
                        parents = self._call(
                            model="res.partner",
                            method="read",
                            args=[[parent_id]],
                            kwargs={"fields": ["name"]},
                        )
                        if parents and isinstance(parents, list):
                            company_name = parents[0].get("name")
                            company_id = parent_id
                    # 3. If partner has a company_id, use that company's name
                    elif partner.get("company_id"):
                        comp_id = partner["company_id"]
                        if isinstance(comp_id, (list, tuple)):
                            comp_id = comp_id[0]
                        companies = self._call(
                            model="res.company",
                            method="read",
                            args=[[comp_id]],
                            kwargs={"fields": ["name"]},
                        )
                        if companies and isinstance(companies, list):
                            company_name = companies[0].get("name")
                            company_id = comp_id

            return {
                "odoo_uid": uid,
                "display_name": user.get("name", ""),
                "email": user.get("email", ""),
                "company_name": company_name,
                "company_id": company_id,
                "user_type": user_type,
                "language": user.get("lang", "en_US"),
                "partner_id": partner_id,
                "is_company": is_company,
            }

        except Exception as e:
            # Log error but don't raise - profile fetch is not critical
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to fetch user profile for uid {uid}: {e}")
            return None

    # ==================== Price & Verification Operations ====================

    def get_price_quote(
        self,
        partner_id: int,
        product_id: int,
        qty: float,
        uom_id: Optional[int] = None,
        unit_price: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get customer-specific price quote using Odoo's pricelist logic.

        Creates a temporary order + line, reads back the computed discount and
        subtotal, then deletes the temporary order.

        Args:
            partner_id: Customer ID (determines pricelist/discount)
            product_id: Product ID to quote
            qty: Quantity to quote
            uom_id: Optional UOM ID (meter or yard)
            unit_price: Optional unit price override (uses list_price if not set)

        Returns:
            Dict with keys: price, discount, subtotal, price_unit, unit, order_id
            or None if quote fails.
        """
        temp_order_id = None
        try:
            # Create temporary order for the customer
            temp_order_id = self.create_order(
                partner_id, note="Temporary Quote Order"
            )

            # Use provided unit price or fall back to 0 (Odoo will compute)
            price = unit_price if unit_price is not None else 0

            # Add product line to temp order
            line_id = self.add_order_line(
                temp_order_id, product_id, qty, price, uom_id=uom_id
            )

            # Read back the computed values from Odoo
            line = self._call(
                model="sale.order.line",
                method="read",
                args=[[line_id]],
                kwargs={
                    "fields": [
                        "product_uom_qty",
                        "price_unit",
                        "discount",
                        "price_subtotal",
                    ]
                },
            )

            if line:
                l = line[0]
                price_unit = l.get("price_unit", 0)
                discount = l.get("discount", 0)
                subtotal = l.get("price_subtotal", 0)
                actual_price = price_unit * (1 - discount / 100)

                return {
                    "price": actual_price,
                    "price_unit": price_unit,
                    "discount": discount,
                    "subtotal": subtotal,
                    "order_id": temp_order_id,
                }

            return None

        except Exception as e:
            logger.error(f"Failed to get price quote: {e}")
            return None

        finally:
            # Always clean up the temporary order
            if temp_order_id:
                try:
                    self._call(
                        model="sale.order",
                        method="unlink",
                        args=[[temp_order_id]],
                    )
                except Exception:
                    pass

    def verify_order(self, order_id: int) -> Optional[Dict[str, Any]]:
        """Verify order line totals against independent recalculation.

        Reads order + all lines, independently recalculates each line's
        subtotal, and compares against Odoo's computed values.

        Args:
            order_id: Order ID to verify

        Returns:
            Dict with keys:
                - order_id: The verified order ID
                - order_total: Odoo's amount_total
                - calculated_total: Sum of line subtotals
                - passed: True if all checks pass
                - lines: List of per-line verification results
            or None if order not found.
        """
        order = self._call(
            model="sale.order",
            method="read",
            args=[[order_id]],
            kwargs={
                "fields": [
                    "order_line",
                    "partner_id",
                    "amount_total",
                    "amount_untaxed",
                ]
            },
        )

        if not order:
            return None

        o = order[0]
        order_lines = o.get("order_line", [])
        line_results = []
        calculated_total = 0.0

        for line_id in order_lines:
            line = self._call(
                model="sale.order.line",
                method="read",
                args=[[line_id]],
                kwargs={
                    "fields": [
                        "product_id",
                        "product_uom_qty",
                        "price_unit",
                        "discount",
                        "price_subtotal",
                        "product_uom",
                    ]
                },
            )
            if not line:
                continue

            l = line[0]
            product_name = (
                l.get("product_id", [None, ""])[1]
                if l.get("product_id")
                else "N/A"
            )
            qty = l.get("product_uom_qty", 0)
            price = l.get("price_unit", 0)
            discount = l.get("discount", 0)
            subtotal = l.get("price_subtotal", 0)

            calc = qty * price * (1 - discount / 100)
            calculated_total += subtotal

            line_results.append({
                "line_id": line_id,
                "product_name": product_name,
                "qty": qty,
                "price": price,
                "discount": discount,
                "subtotal": subtotal,
                "calculated": calc,
                "diff": abs(subtotal - calc),
                "passed": abs(subtotal - calc) < 0.01,
            })

        order_total = o.get("amount_total", 0)
        total_passed = abs(order_total - calculated_total) < 0.01

        return {
            "order_id": order_id,
            "order_total": order_total,
            "calculated_total": calculated_total,
            "total_diff": abs(order_total - calculated_total),
            "passed": total_passed and all(lr["passed"] for lr in line_results),
            "lines": line_results,
        }
