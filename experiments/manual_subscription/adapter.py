"""Offline auth-boundary model. NOT production authentication or an HTTP API."""
from dataclasses import dataclass
import secrets


@dataclass(frozen=True)
class Principal:
    uid: int


class FixtureSessions:
    """In-memory synthetic sessions; issue() is test setup, never a public route."""
    def __init__(self):
        self._sessions = {}

    def issue(self, uid):
        if type(uid) is not int or uid <= 0:
            raise ValueError("Invalid fixture principal")
        token = secrets.token_urlsafe(24)
        self._sessions[token] = Principal(uid)
        return token

    def resolve(self, token):
        if not isinstance(token, str) or token not in self._sessions:
            raise PermissionError("Authenticated fixture session required")
        return self._sessions[token]

    def revoke(self, token):
        self._sessions.pop(token, None)


class Adapter:
    def __init__(self, ledger, sessions, *, tariff_amount=249, tariff_days=30):
        self.ledger, self.sessions = ledger, sessions
        self.tariff_amount, self.tariff_days = tariff_amount, tariff_days

    @staticmethod
    def _body(data, required):
        if type(data) is not dict or set(data) != set(required):
            raise ValueError("Only documented fields are accepted")

    def create(self, token, data, now):
        principal = self.sessions.resolve(token)
        self._body(data, ())  # uid, actor, tariff and expiry cannot come from client input.
        return self.ledger.create_order(principal.uid, self.tariff_amount, now, self.tariff_days)

    def submit(self, token, order_id, data, now):
        principal = self.sessions.resolve(token)
        self._body(data, ("receipt_ref", "paid_at"))
        self.ledger.submit_receipt(principal.uid, order_id, data["receipt_ref"], now, paid_at=data["paid_at"])

    def status(self, token, order_id):
        return self.ledger.order_status(self.sessions.resolve(token).uid, order_id)

    def _admin(self, token):
        principal = self.sessions.resolve(token)
        if principal.uid != self.ledger.admin_id:
            raise PermissionError("Admin session required")
        return principal

    def clarify(self, token, order_id, data, now):
        principal = self._admin(token)
        self._body(data, ("note",))
        self.ledger.clarify(principal.uid, order_id, data["note"], now)

    def approve(self, token, order_id, data, now):
        principal = self._admin(token)
        self._body(data, ("payment_ref", "actual_amount", "bank_verified"))
        return self.ledger.approve(principal.uid, order_id, now=now, **data)

    def reject(self, token, order_id, data, now):
        principal = self._admin(token)
        self._body(data, ())
        self.ledger.reject(principal.uid, order_id, now)

    def review_queue(self, token):
        self._admin(token)
        return self.ledger.db.execute(
            "SELECT id,uid,amount,receipt FROM orders WHERE state='review' ORDER BY created,id LIMIT 100"
        ).fetchall()
