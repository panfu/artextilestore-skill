"""
Portal API Client - User Defaults API
======================================
Client for fetching user default settings from Portal API.

Reference: /Users/astra/conductor/workspaces/ar-odoo-sync/amsterdam-v1/docs/api/user-defaults-api.md
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


class PortalAPIError(Exception):
    """Portal API error."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class UserDefaults:
    """User default settings for order creation."""

    partner_id: int
    shipping_address_id: Optional[int] = None
    shipping_address_name: Optional[str] = None
    payment_term_id: Optional[int] = None
    payment_term_name: Optional[str] = None
    shipping_method: Optional[str] = None
    freight_payment: Optional[str] = None  # "COD" or "Prepaid"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserDefaults":
        """Create UserDefaults from API response dict."""
        return cls(
            partner_id=data.get("partner_id", 0),
            shipping_address_id=data.get("default_shipping_address_id"),
            shipping_address_name=data.get("default_shipping_address_name"),
            payment_term_id=data.get("default_payment_term_id"),
            payment_term_name=data.get("default_payment_term_name"),
            shipping_method=data.get("default_shipping_method"),
            freight_payment=data.get("default_freight_payment"),
        )

    def has_defaults(self) -> bool:
        """Check if any defaults are available."""
        return any([
            self.shipping_address_id,
            self.payment_term_id,
            self.shipping_method,
            self.freight_payment,
        ])

    def to_display_text(self, language: str = "zh") -> str:
        """Format defaults for display to user.

        Presents the settings as the order's own configuration without
        mentioning they are "defaults" or "presets".

        Args:
            language: "zh" for Chinese, "en" for English

        Returns:
            Formatted text for display, or empty string if no defaults
        """
        if language == "zh":
            lines = []
            if self.shipping_address_name:
                lines.append(f"- 收貨地址：{self.shipping_address_name}")
            if self.payment_term_name:
                lines.append(f"- 付款條款：{self.payment_term_name}")
            if self.shipping_method:
                lines.append(f"- 物流方式：{self.shipping_method}")
            if self.freight_payment:
                freight_text = "到付" if self.freight_payment == "COD" else "寄付"
                lines.append(f"- 運費支付：{freight_text}")

            if lines:
                return "訂單配置如下：\n" + "\n".join(lines)
            return ""

        else:  # English
            lines = []
            if self.shipping_address_name:
                lines.append(f"- Shipping Address: {self.shipping_address_name}")
            if self.payment_term_name:
                lines.append(f"- Payment Term: {self.payment_term_name}")
            if self.shipping_method:
                lines.append(f"- Shipping Method: {self.shipping_method}")
            if self.freight_payment:
                lines.append(f"- Freight Payment: {self.freight_payment}")

            if lines:
                return "Order settings:\n" + "\n".join(lines)
            return ""


class PortalAPIClient:
    """
    Client for Portal API.

    Handles fetching user default settings for order creation.
    """

    def __init__(
        self,
        base_url: str,
        api_token: str,
        timeout: float = 30.0,
    ) -> None:
        """
        Initialize Portal API client.

        Args:
            base_url: Portal API base URL (e.g., https://portal.artextile.store)
            api_token: Bearer token for authentication
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        """Close HTTP client."""
        self._client.close()

    def _get_headers(self) -> Dict[str, str]:
        """Get headers for API requests."""
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
        }

    def get_user_defaults(self, partner_id: int) -> UserDefaults:
        """
        Get user default settings for order creation.

        Args:
            partner_id: Odoo res.partner ID

        Returns:
            UserDefaults object with default settings

        Raises:
            PortalAPIError: If request fails
        """
        url = f"{self.base_url}/api/users/{partner_id}/defaults"

        try:
            response = self._client.get(url, headers=self._get_headers())

            if response.status_code == 404:
                # User not found or defaults not analyzed
                try:
                    error_data = response.json()
                    error_msg = error_data.get("message", "User defaults not found")
                except Exception:
                    error_msg = "User defaults not found"
                raise PortalAPIError(error_msg, status_code=404)

            response.raise_for_status()
            data = response.json()

            logger.info(f"Got user defaults for partner_id={partner_id}")
            return UserDefaults.from_dict(data)

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error getting user defaults: {e}")
            raise PortalAPIError(f"HTTP error: {e}", status_code=e.response.status_code)
        except httpx.HTTPError as e:
            logger.error(f"HTTP error getting user defaults: {e}")
            raise PortalAPIError(f"HTTP error: {e}")

    def check_api_available(self) -> bool:
        """
        Check if Portal API is available and configured.

        Returns:
            True if API is available, False otherwise
        """
        if not self.base_url or not self.api_token:
            return False

        # Simple check - API is considered available if configured
        return True


def get_portal_client() -> Optional[PortalAPIClient]:
    """
    Create a Portal API client from environment variables.

    Returns:
        Configured PortalAPIClient instance, or None if not configured
    """
    import os

    base_url = os.getenv("ODOO_PORTAL_API_BASE_URL", "")
    api_token = os.getenv("ODOO_PORTAL_API_TOKEN", "")

    if not base_url or not api_token:
        logger.debug("Portal API not configured (missing ODOO_PORTAL_API_BASE_URL or ODOO_PORTAL_API_TOKEN)")
        return None

    return PortalAPIClient(base_url=base_url, api_token=api_token)
