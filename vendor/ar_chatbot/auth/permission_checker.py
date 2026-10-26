"""Permission checker for Odoo order operations.

This module provides permission validation for order creation based on user types:
- Portal users: Can only create orders for their own account (partner_id)
- Internal users: Must specify a customer (proxy ordering mode)

Security Policy (STRICT for Portal Users):
- Portal users without partner_id cannot create orders
- Portal users can only create orders for their own partner_id
- Internal users must always specify a customer
- Portal users CANNOT view original prices (only discounted prices)
- Portal users CANNOT modify discounts
- Portal users CANNOT view other customers' information

IMPORTANT: These restrictions are NON-NEGOTIABLE.
Even if a portal user explicitly requests to view original prices,
modify discounts, or view other customer info, the system MUST refuse.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .models import OdooUserBinding

logger = logging.getLogger(__name__)


class RestrictedAction(Enum):
    """Actions that are restricted for portal users."""
    VIEW_ORIGINAL_PRICE = "view_original_price"
    MODIFY_DISCOUNT = "modify_discount"
    VIEW_OTHER_CUSTOMER = "view_other_customer"
    ACCESS_UNAUTHORIZED_DATA = "access_unauthorized_data"


@dataclass
class OrderPermissionResult:
    """Result of order permission check.

    Attributes:
        allowed: Whether the operation is allowed
        reason: Human-readable explanation of the decision
        user_type: The user's type ('internal', 'portal', or None)
        partner_id: The user's associated partner_id (if applicable)
    """
    allowed: bool
    reason: str
    user_type: Optional[str] = None
    partner_id: Optional[int] = None


class OrderPermissionChecker:
    """Checker for order creation permissions.

    This class validates whether a user has permission to create an order
    based on their user type and binding status.
    """

    @staticmethod
    def check_create_order_permission(
        binding: Optional[OdooUserBinding],
        customer_id: Optional[int] = None,
    ) -> OrderPermissionResult:
        """Check if the user has permission to create an order.

        Permission rules:
        1. Unbound users: Not allowed
        2. Portal users without partner_id: Not allowed
        3. Portal users: Only allowed to create orders for their own partner_id
        4. Internal users: Must specify a customer (proxy ordering)

        Args:
            binding: The user's Odoo binding record (None if unbound)
            customer_id: The customer ID to create order for (None if not specified)

        Returns:
            OrderPermissionResult with permission decision and explanation
        """
        # Case 1: User not bound or binding invalid
        if not binding or not binding.is_valid:
            return OrderPermissionResult(
                allowed=False,
                reason=(
                    "No Odoo account is bound. Please bind your account first.\n\n"
                    "💡 You can say \"我要綁定\" or \"bind account\" to get a binding link."
                ),
            )

        # Case 2: Portal user validation
        if binding.user_type == "portal":
            return OrderPermissionChecker._check_portal_user_permission(
                binding, customer_id
            )

        # Case 3: Internal user validation
        elif binding.user_type == "internal":
            return OrderPermissionChecker._check_internal_user_permission(
                binding, customer_id
            )

        # Case 4: Unknown user type
        return OrderPermissionResult(
            allowed=False,
            reason=f"Unknown user type: {binding.user_type}. Please contact support.",
            user_type=binding.user_type,
        )

    @staticmethod
    def _check_portal_user_permission(
        binding: OdooUserBinding,
        customer_id: Optional[int],
    ) -> OrderPermissionResult:
        """Check permission for portal user.

        Portal users can only create orders for their own partner_id.
        """
        # Portal user must have a partner_id
        if not binding.partner_id:
            return OrderPermissionResult(
                allowed=False,
                reason=(
                    f"Your portal account ({binding.display_name}) is not linked "
                    "to a customer record.\n\n"
                    "Please contact your administrator to link your account to a customer."
                ),
                user_type="portal",
            )

        # Portal user can only create orders for their own partner_id
        if customer_id is None:
            # No customer specified - will use their own partner_id
            return OrderPermissionResult(
                allowed=True,
                reason=(
                    f"Order will be created for your account: {binding.company_name or binding.display_name} "
                    f"(Customer ID: {binding.partner_id})"
                ),
                user_type="portal",
                partner_id=binding.partner_id,
            )

        if customer_id != binding.partner_id:
            # Trying to create order for different customer
            return OrderPermissionResult(
                allowed=False,
                reason=(
                    f"Permission denied. As a portal user, you can only create orders "
                    f"for your own account (Customer ID: {binding.partner_id}).\n\n"
                    f"You cannot create orders for customer ID {customer_id}."
                ),
                user_type="portal",
                partner_id=binding.partner_id,
            )

        # Creating order for their own partner_id
        return OrderPermissionResult(
            allowed=True,
            reason=(
                f"Order will be created for your account: {binding.company_name or binding.display_name} "
                f"(Customer ID: {binding.partner_id})"
            ),
            user_type="portal",
            partner_id=binding.partner_id,
        )

    @staticmethod
    def _check_internal_user_permission(
        binding: OdooUserBinding,
        customer_id: Optional[int],
    ) -> OrderPermissionResult:
        """Check permission for internal user.

        Internal users must specify a customer for proxy ordering.
        """
        # Internal user must specify a customer
        if customer_id is None:
            return OrderPermissionResult(
                allowed=False,
                reason=(
                    "As an internal user, you must specify which customer to create the order for.\n\n"
                    "Please specify a customer ID or name when creating an order.\n"
                    "Example: create_order(customer='華遠') or create_order(customer='10640')"
                ),
                user_type="internal",
            )

        # Internal user can create orders for any customer
        return OrderPermissionResult(
            allowed=True,
            reason=f"Order will be created for customer ID: {customer_id}",
            user_type="internal",
        )


class PortalRestrictionChecker:
    """Strict permission checker for portal user restrictions.

    Portal users operate through the internal user account but have
    STRICT limitations on what they can see and do.

    IRONCLAD RULES (Never to be bypassed):
    1. Portal users CANNOT view original prices (list_price)
    2. Portal users CANNOT modify discounts
    3. Portal users CANNOT view other customers' information

    These rules apply even if the user explicitly requests otherwise.
    """

    # Denial messages for portal users
    DENIAL_MESSAGES = {
        RestrictedAction.VIEW_ORIGINAL_PRICE: (
            "抱歉，您無法查看產品原價。如有價格疑問，請聯繫客服。"
            "\n\n(系統已為您應用最優價格)"
        ),
        RestrictedAction.MODIFY_DISCOUNT: (
            "抱歉，您無法修改折扣。系統已為您應用最優價格。"
            "\n\n(如有特殊折扣需求，請聯繫銷售代表)"
        ),
        RestrictedAction.VIEW_OTHER_CUSTOMER: (
            "抱歉，您只能查看自己的客戶資訊。"
            "\n\n(出於隱私保護，無法訪問其他客戶數據)"
        ),
        RestrictedAction.ACCESS_UNAUTHORIZED_DATA: (
            "抱歉，您沒有權限訪問此資訊。"
        ),
    }

    def __init__(self, binding: Optional[OdooUserBinding]):
        """Initialize the restriction checker.

        Args:
            binding: The user's Odoo binding record
        """
        self.binding = binding
        self._is_portal = binding and binding.user_type == "portal"
        self._partner_id = binding.partner_id if binding else None

    @property
    def is_portal_user(self) -> bool:
        """Check if current user is a portal user."""
        return self._is_portal

    @property
    def is_internal_user(self) -> bool:
        """Check if current user is an internal user."""
        return self.binding and self.binding.user_type == "internal"

    def can_view_original_price(self) -> bool:
        """Check if user can view original (undiscounted) price.

        Portal users can NEVER view original prices.
        """
        if self.is_internal_user:
            return True
        if self._is_portal:
            logger.warning(
                f"[PortalRestriction] Portal user attempted to view original price. "
                f"partner_id={self._partner_id}"
            )
        return False

    def can_modify_discount(self) -> bool:
        """Check if user can modify discount rates.

        Portal users can NEVER modify discounts.
        """
        if self.is_internal_user:
            return True
        if self._is_portal:
            logger.warning(
                f"[PortalRestriction] Portal user attempted to modify discount. "
                f"partner_id={self._partner_id}"
            )
        return False

    def can_access_customer(self, target_customer_id: int) -> bool:
        """Check if user can access the specified customer's data.

        Portal users can ONLY access their own customer data.
        """
        if self.is_internal_user:
            return True
        if self._is_portal:
            if self._partner_id and target_customer_id == self._partner_id:
                return True
            logger.warning(
                f"[PortalRestriction] Portal user attempted to access other customer. "
                f"own_partner_id={self._partner_id}, target_customer_id={target_customer_id}"
            )
            return False
        return False

    def get_denial_message(self, action: RestrictedAction) -> str:
        """Get the denial message for a restricted action."""
        return self.DENIAL_MESSAGES.get(
            action,
            "抱歉，此操作不被允許。"
        )

    def check_and_raise(self, action: RestrictedAction) -> None:
        """Check permission and raise exception if denied.

        Args:
            action: The action to check

        Raises:
            PermissionError: If the action is not allowed
        """
        if self._is_portal:
            allowed_actions = {
                RestrictedAction.VIEW_ORIGINAL_PRICE: self.can_view_original_price,
                RestrictedAction.MODIFY_DISCOUNT: self.can_modify_discount,
            }
            checker = allowed_actions.get(action)
            if checker and not checker():
                raise PermissionError(self.get_denial_message(action))


def check_portal_restriction(binding: Optional[OdooUserBinding]) -> PortalRestrictionChecker:
    """Factory function to create a PortalRestrictionChecker.

    Args:
        binding: The user's Odoo binding record

    Returns:
        PortalRestrictionChecker instance
    """
    return PortalRestrictionChecker(binding)


# Convenience functions for quick checks
def is_portal_user(binding: Optional[OdooUserBinding]) -> bool:
    """Quick check if user is a portal user."""
    return binding and binding.user_type == "portal"


def can_view_original_price(binding: Optional[OdooUserBinding]) -> bool:
    """Quick check if user can view original prices."""
    return PortalRestrictionChecker(binding).can_view_original_price()


def can_modify_discount(binding: Optional[OdooUserBinding]) -> bool:
    """Quick check if user can modify discounts."""
    return PortalRestrictionChecker(binding).can_modify_discount()
