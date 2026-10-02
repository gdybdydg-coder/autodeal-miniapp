"""Additive manual-review storage; no sales flags or synthetic data migration."""
from sqlalchemy import BigInteger, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ManualBase(DeclarativeBase):
    pass


class PaymentRequest(ManualBase):
    __tablename__ = "manual_payment_requests"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    active_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(100), default="Клієнт")
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount_minor: Mapped[int] = mapped_column(Integer, default=25000)
    currency: Mapped[str] = mapped_column(String(3), default="UAH")
    days: Mapped[int] = mapped_column(Integer, default=30)
    state: Mapped[str] = mapped_column(String(20), default="created", index=True)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    reported_amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transfer_note: Mapped[str] = mapped_column(String(500), default="")
    receipt_file_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    receipt_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    owner_note: Mapped[str] = mapped_column(String(500), default="")
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    terms_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    terms_text: Mapped[str | None] = mapped_column(String(2500), nullable=True)
    terms_accepted_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class PaymentConfirmation(ManualBase):
    __tablename__ = "manual_payment_confirmations"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(20), index=True)
    actor: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(Integer)
    before_expiry: Mapped[float] = mapped_column(Float)
    bank_key: Mapped[str] = mapped_column(String(64))
    amount_minor: Mapped[int] = mapped_column(Integer)
    deadline: Mapped[float] = mapped_column(Float)
    result: Mapped[float | None] = mapped_column(Float, nullable=True)


class PaymentEvidence(ManualBase):
    __tablename__ = "manual_payment_evidence"
    request_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[float] = mapped_column(Float)
    reported_amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transfer_note: Mapped[str] = mapped_column(String(500))
    receipt_file_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    receipt_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)


class BankCredit(ManualBase):
    __tablename__ = "manual_bank_credits"
    bank_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(20), unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    amount_minor: Mapped[int] = mapped_column(Integer)
    actor: Mapped[int] = mapped_column(BigInteger)
    at: Mapped[float] = mapped_column(Float)
    before_expiry: Mapped[float] = mapped_column(Float)
    after_expiry: Mapped[float] = mapped_column(Float)


class PaymentAudit(ManualBase):
    __tablename__ = "manual_payment_audit"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(20), index=True)
    actor: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(30))
    at: Mapped[float] = mapped_column(Float)
    revision: Mapped[int] = mapped_column(Integer)
    before_expiry: Mapped[float | None] = mapped_column(Float, nullable=True)
    after_expiry: Mapped[float | None] = mapped_column(Float, nullable=True)


class PaymentNotice(ManualBase):
    __tablename__ = "manual_payment_notices"
    __table_args__ = (UniqueConstraint("request_id", "revision", "kind"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    user_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(String(2000))
    state: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempted_at: Mapped[float] = mapped_column(Float, default=0)
    retry_at: Mapped[float] = mapped_column(Float, default=0)
    claim: Mapped[str] = mapped_column(String(32), default="")
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
