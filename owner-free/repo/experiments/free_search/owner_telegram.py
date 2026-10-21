"""Explicit one-shot Telegram transport; no env loading, retries or polling.

Not constructed by public collectors. Keep credentials outside their process.
Exceptions deliberately omit the request URL because it contains a bot token.
"""
import re


class TelegramText:
    def __init__(self, token, owner_id):
        if (not isinstance(token, str) or not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}', token)
                or type(owner_id) is not int or owner_id <= 0):
            raise ValueError('invalid_owner_transport_configuration')
        self._token, self._owner = token, owner_id

    def __call__(self, recipient, text):
        if recipient != self._owner or not isinstance(text, str) or not 1 <= len(text) <= 4096:
            raise ValueError('invalid_owner_message')
        import httpx
        try:
            with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
                with client.stream('POST', 'https://api.telegram.org/bot'+self._token+'/sendMessage',
                    json={'chat_id': self._owner, 'text': text, 'parse_mode': 'HTML',
                          'link_preview_options': {'is_disabled': True}}) as response:
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 65536:
                            return {'uncertain': True}
                    import json
                    data = json.loads(body)
                    message = data.get('result') if isinstance(data, dict) else None
                    if (response.status_code == 200 and isinstance(message, dict)
                            and (message.get('chat') or {}).get('id') == self._owner):
                        return data
        except Exception:
            pass
        return {'uncertain': True}
