"""Additive commercial tables. Never migrate synthetic trials into paid access."""
from sqlalchemy import BigInteger, Boolean, Float, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base


class BillingControl(Base):
    __tablename__ = "billing_control"
    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    sales: Mapped[bool] = mapped_column(Boolean, default=False)
    enforce: Mapped[bool] = mapped_column(Boolean, default=False)
    offer: Mapped[dict] = mapped_column(JSON, default=dict)


class Entitlement(Base):
    __tablename__ = "billing_entitlements"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    expires_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class BillingOrder(Base):
    __tablename__ = "billing_orders"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    update_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    amount: Mapped[int] = mapped_column(Integer)
    terms_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String(30), default="pending")
    charge_id: Mapped[str | None] = mapped_column(String(256), unique=True, nullable=True)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class AccessEvent(Base):
    __tablename__ = "billing_access_events"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    actor: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(30))
    at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(500))


class MarketingConsent(Base):
    __tablename__ = "marketing_consents"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    update_id: Mapped[int] = mapped_column(BigInteger, default=-1)
    at: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(80))


class BillingCampaign(Base):
    __tablename__ = "billing_campaigns"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    not_before: Mapped[float] = mapped_column(Float)
    deadline: Mapped[float] = mapped_column(Float)
    timezone: Mapped[str] = mapped_column(String(40))
    content: Mapped[dict] = mapped_column(JSON)
    audience: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30))
    blockers: Mapped[list] = mapped_column(JSON, default=list)
    heartbeat: Mapped[float] = mapped_column(Float, default=0)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    lease_token: Mapped[str] = mapped_column(String(32), default="")
    next_send: Mapped[float] = mapped_column(Float, default=0)


class CampaignRecipient(Base):
    __tablename__ = "billing_campaign_recipients"
    campaign_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    state: Mapped[str] = mapped_column(String(30), index=True)
    attempted_at: Mapped[float] = mapped_column(Float, default=0)
    retry_at: Mapped[float] = mapped_column(Float, default=0)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str] = mapped_column(String(60), default="")


class BillingNotice(Base):
    __tablename__ = "billing_notices"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    user_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(String(4000))
    state: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    attempted_at: Mapped[float] = mapped_column(Float, default=0)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    retry_at: Mapped[float] = mapped_column(Float, default=0)


class TariffReminderSchedule(Base):
    """One durable schedule; recipient queues reuse BillingCampaign tables."""
    __tablename__ = "tariff_reminder_schedule"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_update_id: Mapped[int] = mapped_column(BigInteger, default=-1)
    installed_at: Mapped[float] = mapped_column(Float)
    first_run_at: Mapped[float] = mapped_column(Float)
    next_run_at: Mapped[float] = mapped_column(Float)
    heartbeat: Mapped[float] = mapped_column(Float, default=0)
    last_campaign_id: Mapped[str] = mapped_column(String(64), default="")
    last_result: Mapped[dict] = mapped_column(JSON, default=dict)


class TariffReminderPreference(Base):
    """Explicit category choice; never changes access, filters or global consent."""
    __tablename__ = "tariff_reminder_preferences"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    update_id: Mapped[int] = mapped_column(BigInteger, default=-1)
    at: Mapped[float] = mapped_column(Float)
