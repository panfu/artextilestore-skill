"""Data models for Odoo-Lark/WeChat user bindings.

This module defines the data structures used for storing and managing
user bindings between Lark/WeChat and Odoo.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Literal
import os
import secrets
import warnings


PlatformType = Literal["lark", "wechat", "gmail"]


def mask_email(email: str) -> str:
    """Mask email keeping first 2 and last 2 characters.

    Args:
        email: Email address to mask

    Returns:
        Masked email in format: fr*****an@domain.com

    Examples:
        >>> mask_email("fred.pan@artextile.com")
        'fr*****an@artextile.com'
        >>> mask_email("ab@cd.com")
        '**@cd.com'
    """
    if "@" not in email:
        return email

    local, domain = email.split("@", 1)

    if len(local) <= 4:
        masked = "*" * len(local)
    else:
        masked = f"{local[:2]}{'*' * (len(local) - 4)}{local[-2:]}"

    return f"{masked}@{domain}"


@dataclass
class OdooUserProfile:
    """User profile fetched from Odoo.

    Attributes:
        odoo_uid: Odoo user ID
        display_name: User's full name
        email: Full email address
        email_masked: Masked email for display
        company_name: Company name (if available)
        company_id: Company ID (if available)
        user_type: 'internal' or 'portal'
        language: Language code (e.g., 'zh_CN', 'en_US')
        partner_id: Res.partner ID
        is_company: Whether the partner is a company
    """
    odoo_uid: int
    display_name: str
    email: str
    email_masked: str
    company_name: Optional[str] = None
    company_id: Optional[int] = None
    user_type: str = "internal"
    language: str = "en_US"
    partner_id: Optional[int] = None
    is_company: bool = False


@dataclass
class OdooUserBinding:
    """Represents a Platform-Odoo user binding.

    Attributes:
        platform_type: Platform type ('lark' or 'wechat')
        platform_user_id: Platform user identifier (open_id for Lark, openid for WeChat)
        odoo_username: Odoo login username
        odoo_password_encrypted: AES-256 encrypted password (DEPRECATED: no longer stored for security)
        odoo_uid: Cached Odoo user ID from login
        odoo_db: Odoo database name
        is_active: Whether the binding is active (for soft deletion)
        created_at: When the binding was created
        updated_at: When the binding was last updated
        last_verified_at: Last successful credential verification
        verification_status: Status of verification (pending, verified, failed)
        display_name: User's display name from Odoo
        company_name: User's company name
        email: User's email address
        email_masked: Masked email for display (e.g., fr*****an@domain.com)
        user_type: User type ('internal' or 'portal')
        language: User's language code
        partner_id: Linked res.partner ID
        profile_fetched_at: When the profile was last fetched from Odoo
        odoo_session_id: Odoo session ID (cookie value) for persistent authentication
        odoo_session_expires_at: Odoo session expiration timestamp
        nickname: Name the USER asked to be addressed by ("怎么称呼您" flow).
            Distinct from display_name, which is the Odoo account name.
        nickname_asked_at: When the nickname question was sent. Cross-process
            pending marker: nickname IS NULL + asked recently = awaiting answer.
    """

    platform_type: PlatformType
    platform_user_id: str
    odoo_username: str
    odoo_password_encrypted: str  # DEPRECATED: Fully removed. Kept for backward compat with existing DB rows only.
    odoo_uid: Optional[int] = None
    odoo_db: str = ""
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_verified_at: Optional[datetime] = None
    verification_status: str = "pending"
    display_name: Optional[str] = None
    company_name: Optional[str] = None
    email: Optional[str] = None
    email_masked: Optional[str] = None
    user_type: str = "internal"
    language: Optional[str] = None
    partner_id: Optional[int] = None
    company_id: Optional[int] = None
    profile_fetched_at: Optional[datetime] = None
    is_cs: bool = False
    odoo_session_id: Optional[str] = None  # DEPRECATED: Session-based auth removed. Kept for backward compat with existing DB rows.
    odoo_session_expires_at: Optional[datetime] = None  # DEPRECATED: Session-based auth removed.
    # How the user asked to be addressed (WeChat "怎么称呼您" flow). Survives
    # re-binds — it identifies the human, not the Odoo account.
    nickname: Optional[str] = None
    nickname_asked_at: Optional[datetime] = None

    @property
    def lark_open_id(self) -> str:
        """Legacy property for backward compatibility."""
        return self.platform_user_id if self.platform_type == "lark" else ""

    def to_dict(self, include_credentials: bool = False) -> dict:
        """Convert binding to dictionary (excluding sensitive data by default).

        Args:
            include_credentials: Whether to include encrypted password

        Returns:
            Dictionary representation of the binding
        """
        result = {
            "platform_type": self.platform_type,
            "platform_user_id": self.platform_user_id,
            "lark_open_id": self.platform_user_id if self.platform_type == "lark" else None,  # Legacy
            "odoo_username": self.odoo_username,
            "odoo_uid": self.odoo_uid,
            "odoo_db": self.odoo_db,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_verified_at": (
                self.last_verified_at.isoformat() if self.last_verified_at else None
            ),
            "verification_status": self.verification_status,
            "display_name": self.display_name,
            "company_name": self.company_name,
            "email_masked": self.email_masked,
            "user_type": self.user_type,
            "language": self.language,
            "partner_id": self.partner_id,
            "is_cs": self.is_cs,
            "nickname": self.nickname,
        }
        if include_credentials:
            result["odoo_password_encrypted"] = self.odoo_password_encrypted
            result["email"] = self.email
        return result

    @property
    def is_valid(self) -> bool:
        """Check if the binding is valid, active, and not expired.

        A binding is valid when:
        1. is_active is True
        2. verification_status is "verified"
        3. last_verified_at is within BINDING_VALIDITY_DAYS (default 30 days)

        If last_verified_at is None, falls back to updated_at.
        If neither timestamp exists, the binding is considered expired.

        Returns:
            True if binding is active, verified, and not expired
        """
        if not self.is_active or self.verification_status != "verified":
            return False

        # Determine the reference timestamp for validity
        reference_time = self.last_verified_at or self.updated_at
        if reference_time is None:
            return False

        # Naive datetimes are treated as UTC
        if reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=timezone.utc)

        try:
            validity_days = int(os.getenv("BINDING_VALIDITY_DAYS", "30"))
        except (ValueError, TypeError):
            import logging
            logging.getLogger(__name__).warning(
                "Invalid BINDING_VALIDITY_DAYS env value, falling back to 30"
            )
            validity_days = 30
        now = datetime.now(timezone.utc)
        return (now - reference_time) <= timedelta(days=validity_days)




@dataclass
class BindingToken:
    """Token for managing the binding flow.

    Tokens are single-use and expire after a set time period.

    Attributes:
        token: Secure random token string
        platform_type: Platform type ('lark' or 'wechat')
        platform_user_id: Platform user ID attempting to bind
        expires_at: Token expiration timestamp
        created_at: When the token was created
        used_at: When the token was used (None if unused)
    """

    token: str
    platform_type: PlatformType
    platform_user_id: str
    expires_at: datetime
    created_at: Optional[datetime] = None
    used_at: Optional[datetime] = None

    @property
    def lark_open_id(self) -> str:
        """Legacy property for backward compatibility."""
        return self.platform_user_id if self.platform_type == "lark" else ""

    @classmethod
    def create(
        cls, platform_type: PlatformType, platform_user_id: str, expires_minutes: int = 15
    ) -> "BindingToken":
        """Create a new binding token.

        Args:
            platform_type: Platform type ('lark' or 'wechat')
            platform_user_id: Platform user ID
            expires_minutes: Minutes until token expires (default: 15)

        Returns:
            New BindingToken instance
        """
        now = datetime.now()
        return cls(
            token=secrets.token_urlsafe(48),  # 64-character token
            platform_type=platform_type,
            platform_user_id=platform_user_id,
            expires_at=now + timedelta(minutes=expires_minutes),
            created_at=now,
        )

    def is_valid(self) -> bool:
        """Check if the token is valid (unused and not expired).

        Returns:
            True if token is unused and not expired
        """
        if self.used_at is not None:
            return False
        return datetime.now() < self.expires_at

    def is_expired(self) -> bool:
        """Check if the token is expired.

        Returns:
            True if token has expired
        """
        return datetime.now() >= self.expires_at

    def remaining_minutes(self) -> float:
        """Get remaining minutes until expiration.

        Returns:
            Minutes remaining (0 if expired)
        """
        remaining = self.expires_at - datetime.now()
        return max(0, remaining.total_seconds() / 60)
