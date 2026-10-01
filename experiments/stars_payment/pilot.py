"""Offline owner-only Stars pilot. No Telegram transport or production imports."""
import secrets
import sqlite3


class Pilot:
    def __init__(self, path, owner_id, *, enabled=False):
        if type(owner_id) is not int or owner_id <= 0:
            raise ValueError("Explicit owner ID required")
        self.owner_id, self.enabled = owner_id, enabled
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS orders (
                payload TEXT PRIMARY KEY, owner INTEGER NOT NULL,
                created INTEGER NOT NULL, state TEXT NOT NULL,
                charge TEXT UNIQUE, paid_until INTEGER);
        ''')

    def visible(self, uid, chat_id):
        return (self.enabled and type(uid) is int and type(chat_id) is int
                and uid == chat_id == self.owner_id)

    def invoice(self, uid, chat_id, now):
        if not self.visible(uid, chat_id):
            return None
        payload = "autodeal-pilot:" + secrets.token_hex(16)
        with self.db:
            self.db.execute("INSERT INTO orders VALUES (?, ?, ?, 'pending', NULL, NULL)",
                            (payload, uid, now))
        return {"chat_id": uid, "title": "AUTODeal — тест оплати",
                "description": "Тестовий рахунок. Не змінює доступ до пошуку авто.",
                "payload": payload, "currency": "XTR", "provider_token": "",
                "prices": [{"label": "Тест", "amount": 1}],
                "start_parameter": "owner-payment-pilot"}

    def _valid(self, uid, chat_id, payment, now):
        if (not self.visible(uid, chat_id) or payment.get("currency") != "XTR"
                or type(payment.get("total_amount")) is not int
                or payment["total_amount"] != 1):
            return None
        row = self.db.execute("SELECT * FROM orders WHERE payload=?",
                              (payment.get("invoice_payload"),)).fetchone()
        if not row or row[1] != uid or not 0 <= now-row[2] <= 900:
            return None
        return row

    def precheckout(self, uid, payment, now):
        row = self._valid(uid, uid, payment, now)
        return bool(row and row[3] == "pending")

    def successful_payment(self, uid, chat_id, payment, now):
        """Caller must authenticate Telegram webhook before invoking this method."""
        charge = payment.get("telegram_payment_charge_id")
        if not isinstance(charge, str) or not charge:
            return "rejected"
        # Serialize before checking and recording: concurrent duplicate events
        # cannot grant the simulated entitlement twice.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._valid(uid, chat_id, payment, now)
            if row and row[3] == "paid" and row[4] == charge:
                return "duplicate"
            if not row or row[3] != "pending":
                return "rejected"
            existing = self.db.execute("SELECT 1 FROM orders WHERE charge=?", (charge,)).fetchone()
            if existing:
                return "rejected"
            self.db.execute("UPDATE orders SET state='paid', charge=?, paid_until=? WHERE payload=?",
                            (charge, now+30*86400, row[0]))
        return "recorded_test_only"

    def close(self):
        self.db.close()
