"""Balance/credit sufficiency check (客服-side only, NEVER customer-facing).

Keyed on the partner's payment term (design doc §6, 2026-09-20):

- ``COD-DEPO`` (is_cod term whose name contains DEPO) → **deposit** check:
  available = ``-total_due - pending_sales``. Due Amount 负数 = 客户剩余预存款;
  订料单 (confirmed uninvoiced orders = ``total_pending_sales_credit``) hit
  ``total_due`` only at invoice time, so they are subtracted explicitly.
- non-COD account terms (CM+10D/CM+70D/CM+100D/CM+160D/7/15/30 Days) →
  **credit** check: ``remaining_credit`` already nets 订料单
  (``credit_limit - credit - pending_sales``) — subtracting again would
  double-count. Same口径 as the ``is_limit_exceed`` flag 客服 uses.
- ``COD`` / ``COD-GO`` / empty term → **skipped** (pay-per-order customers;
  the check would only create 客服 noise).

The result NEVER blocks confirmation — the customer's "1" advances the order
exactly as today. Its only consumer is the WeChat 导航面板 node
「余额不足需人工审核」 plus the ``balance`` snapshot in PENDING_QUOTATION meta.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Float-noise guard: amounts equal within half a cent count as equal.
_EPSILON = 0.005

STATE_OK = "ok"
STATE_INSUFFICIENT = "insufficient"
STATE_SKIPPED = "skipped"

KIND_DEPOSIT = "deposit"
KIND_CREDIT = "credit"


@dataclass(frozen=True)
class BalanceCheck:
    """Outcome of one sufficiency check.

    ``available`` is the 客服-facing number (deposit: 可用预存款 = −due −订料单;
    credit: remaining_credit). ``state == skipped`` leaves kind/available None.
    """

    state: str
    order_amount: float
    kind: Optional[str] = None
    available: Optional[float] = None
    term_name: str = ""


def classify_term(term_name: Optional[str], term_is_cod: Optional[bool]) -> Optional[str]:
    """Map a payment term to a check kind. None = skip the check.

    ``term_is_cod`` comes from ``account.payment.term.is_cod`` (credit_limit_jsi).
    When the flag is unavailable (production Odoo without the module), fall
    back to name heuristics only.
    """
    name = (term_name or "").strip().upper()
    if not name:
        return None
    if term_is_cod is True:
        return KIND_DEPOSIT if "DEPO" in name else None
    if term_is_cod is False:
        return KIND_CREDIT
    # Flag unknown — name-only heuristic.
    if "DEPO" in name:
        return KIND_DEPOSIT
    if "COD" in name:
        return None
    return KIND_CREDIT


def check_balance(profile: dict, order_amount: float) -> BalanceCheck:
    """Run the sufficiency check for one (pre-confirm) order amount.

    ``profile`` is ``odoo_client.get_partner_payment_profile()`` output:
    ``term_name`` / ``term_is_cod`` / ``total_due`` / ``remaining_credit`` /
    ``pending_sales``.
    """
    term_name = (profile.get("term_name") or "").strip()
    kind = classify_term(term_name, profile.get("term_is_cod"))
    if kind is None:
        return BalanceCheck(
            state=STATE_SKIPPED, order_amount=order_amount, term_name=term_name
        )
    if kind == KIND_DEPOSIT:
        available = -float(profile.get("total_due") or 0.0) - float(
            profile.get("pending_sales") or 0.0
        )
    else:
        available = float(profile.get("remaining_credit") or 0.0)
    state = (
        STATE_INSUFFICIENT
        if order_amount > available + _EPSILON
        else STATE_OK
    )
    return BalanceCheck(
        state=state,
        order_amount=order_amount,
        kind=kind,
        available=available,
        term_name=term_name,
    )


def post_confirm_insufficient(profile: dict) -> bool:
    """Re-check AFTER the order is confirmed (state sent/sale, invoice_count=0).

    The confirmed order itself now sits inside ``pending_sales`` /
``remaining_credit``, so the question changes from "does THIS order fit" to
"do all current commitments fit the balance" — mirroring the non-draft
branch of ``credit_limit_jsi``'s ``is_limit_exceed`` (remaining_credit < 0).

    Used to decide whether the 余额不足 marker stays on the 导航面板.
    """
    kind = classify_term(profile.get("term_name"), profile.get("term_is_cod"))
    if kind == KIND_DEPOSIT:
        return float(profile.get("pending_sales") or 0.0) > -float(
            profile.get("total_due") or 0.0
        ) + _EPSILON
    if kind == KIND_CREDIT:
        return float(profile.get("remaining_credit") or 0.0) < -_EPSILON
    return False
