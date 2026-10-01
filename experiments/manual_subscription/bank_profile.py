"""Private LOCAL recipient settings; never a bank API or payment authorization."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from tariff import CURRENCY, PAYMENTS_ENABLED, confirmed_snapshot

ROOT = Path(__file__).resolve().parents[2]
MAX_PROFILE_BYTES = 8192
FIELDS = {'schema_version', 'recipient_name', 'recipient_code', 'iban',
          'bank_name', 'mode', 'tariff_confirmed'}


def normalize_iban(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError('Invalid recipient IBAN')
    compact = re.sub(r'[ \t\r\n\u00a0]', '', value).upper()
    # Ukraine: country + two check digits + six-digit institution code + BBAN.
    if not re.fullmatch(r'UA[0-9]{8}[A-Z0-9]{19}', compact, flags=re.ASCII):
        raise ValueError('Invalid recipient IBAN')
    numeric = ''.join(str(ord(c)-55) if 'A' <= c <= 'Z' else c
                      for c in compact[4:]+compact[:4])
    if int(numeric) % 97 != 1:
        raise ValueError('Invalid recipient IBAN checksum')
    return compact


def label(value):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 200
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('Invalid recipient label')
    return value.strip()


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate private recipient field')
        result[key] = value
    return result


@dataclass(frozen=True)
class RecipientProfile:
    recipient_name: str
    recipient_code: str
    iban: str
    bank_name: str

    @classmethod
    def parse(cls, data):
        if type(data) is not dict or set(data) != FIELDS:
            raise ValueError('Invalid private recipient schema')
        if (type(data['schema_version']) is not int or data['schema_version'] != 1
                or data['mode'] != 'test_only' or data['tariff_confirmed'] is not False):
            raise ValueError('Recipient settings cannot approve a tariff or enable payments')
        code = data['recipient_code']
        if not isinstance(code, str) or not re.fullmatch(r'(?:[0-9]{8}|[0-9]{10})', code):
            raise ValueError('Invalid recipient code')
        return cls(label(data['recipient_name']), code, normalize_iban(data['iban']), label(data['bank_name']))

    def instruction(self, order_id, amount, days):
        if not re.fullmatch(r'AD-[A-F0-9]{16}', order_id):
            raise ValueError('Invalid payment order reference')
        if any(type(value) is not int or value <= 0 for value in (amount, days)):
            raise ValueError('Invalid test payment snapshot')
        confirmed = confirmed_snapshot(amount, days)
        notice = (f'Тестовий режим. Тариф погоджено: {amount} грн за {days} днів. Не переказуй кошти.'
                  if confirmed else 'Лише перегляд реквізитів. Ціна цієї тестової заявки не затверджена. Не переказуй кошти.')
        return {'recipient_name': self.recipient_name, 'recipient_code': self.recipient_code,
                'iban': self.iban, 'bank_name': self.bank_name,
                'purpose': f'Доступ до AUTODeal на {days} днів. Заявка {order_id}.',
                'amount': amount, 'currency': CURRENCY, 'days': days,
                'tariff_confirmed': confirmed, 'payments_enabled': PAYMENTS_ENABLED,
                'notice': notice}


def load_profile(path):
    """Read an explicitly selected private file OUTSIDE the git checkout.

    No env scan, auto-discovery, URLs or backend secrets. No values in errors.
    A profile cannot switch on real payment collection or choose a tariff.
    """
    target = Path(path)
    if target.resolve().is_relative_to(ROOT):
        raise ValueError('Private recipient file must stay outside repository')
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        details = os.fstat(fd)
        if (not stat.S_ISREG(details.st_mode) or details.st_mode & 0o077
                or not 0 < details.st_size <= MAX_PROFILE_BYTES):
            raise ValueError('Private recipient file permissions or size invalid')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_PROFILE_BYTES+1)
        if len(raw) > MAX_PROFILE_BYTES:
            raise ValueError('Private recipient file too large')
        return RecipientProfile.parse(json.loads(raw,object_pairs_hook=unique_fields))
    finally:
        os.close(fd)
