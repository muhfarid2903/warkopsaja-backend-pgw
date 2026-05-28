"""
Pydantic models and settings for the MikroTik Payment Backend.

All settings are loaded from environment variables (see .env.example).
Validation is enforced at startup so misconfiguration fails fast.
"""

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Settings (loaded from environment / .env file)
# ---------------------------------------------------------------------------

class MikroTikSettings(BaseSettings):
    """MikroTik router connection settings."""

    model_config = SettingsConfigDict(
        env_prefix="MIKROTIK_",
        extra="ignore",
    )

    host: str
    username: str = "admin"
    password: str
    port: int = 8728  # Default RouterOS API port

    # Backup VPN remote — dipakai otomatis bila primary gagal connect.
    host_backup: str | None = None
    port_backup: int = 8728

    @field_validator("host")
    @classmethod
    def host_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("MIKROTIK_HOST must be set")
        return v

    @field_validator("password")
    @classmethod
    def password_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("MIKROTIK_PASSWORD must be set")
        return v

    @field_validator("host_backup", mode="before")
    @classmethod
    def empty_backup_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v


class PaygatewsSettings(BaseSettings):
    """Paygatews payment gateway settings."""

    model_config = SettingsConfigDict(
        env_prefix="PAYGATEWS_",
        extra="ignore",
    )

    gateway_url: str = "http://localhost:3000"
    api_key: str = ""
    callback_url: str = ""  # URL backend ini yang dipanggil paygatews saat order PAID
    timeout_seconds: int = 10

    @field_validator("api_key")
    @classmethod
    def api_key_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("PAYGATEWS_API_KEY must be set")
        return v


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------

class PaymentRequest(BaseModel):
    """Request body for POST /api/purchase."""

    profile_id: str = Field(..., description="MikroTik hotspot profile ID")

    @field_validator("profile_id")
    @classmethod
    def profile_id_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("profile_id must not be empty")
        return v


class GetStatusRequest(BaseModel):
    """Request body for POST /api/account/create."""

    order_id: str = Field(..., description="Order ID / reference to verify")

    @field_validator("order_id")
    @classmethod
    def order_id_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("order_id must not be empty")
        return v


class PaymentResponse(BaseModel):
    """Response for POST /api/purchase."""

    payment_url: str
    transaction_id: str
    unique_amount: int | None = None


class ProfileResponse(BaseModel):
    """A parsed hotspot profile returned to the frontend."""

    id: str
    name: str
    duration: str
    price: str        # Keep as string so the frontend can format currency
    rate_limit: str | None = None


class AccountResponse(BaseModel):
    """Response for POST /api/account/create — credentials for the new user."""

    user: str
    password: str
    message: str


class HotspotUser(BaseModel):
    """A MikroTik hotspot user (used internally; endpoint is commented out)."""

    id: str
    name: str
    profile: str
    uptime: str | None = None
    bytes_in: str | None = Field(None, alias="bytes-in")
    bytes_out: str | None = Field(None, alias="bytes-out")
    packets_in: str | None = Field(None, alias="packets-in")
    packets_out: str | None = Field(None, alias="packets-out")
    mac_address: str | None = Field(None, alias="mac-address")
    login_by: str | None = Field(None, alias="login-by")
    active: str | None = None
