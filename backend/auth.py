import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def telegram_user(init_data: str, token: str, now: int | None = None) -> int:
    """Validate raw initData, not initDataUnsafe or a client-provided chat ID."""
    if not token or not init_data or len(init_data) > 16384:
        raise ValueError("Invalid Telegram session")
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    data = dict(pairs)
    if len(pairs) != len(data):
        raise ValueError("Duplicate Telegram field")
    received = data.pop("hash", "")
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    payload = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise ValueError("Invalid Telegram signature")
    now = int(time.time()) if now is None else now
    age = now - int(data["auth_date"])
    if age < -30 or age > 3600:
        raise ValueError("Expired Telegram session")
    user = json.loads(data["user"])
    if not isinstance(user, dict):
        raise ValueError("Invalid Telegram user")
    user_id = user.get("id")
    if type(user_id) is not int or not 0 < user_id < 2**52 or user.get("is_bot"):
        raise ValueError("Invalid Telegram user")
    return user_id
