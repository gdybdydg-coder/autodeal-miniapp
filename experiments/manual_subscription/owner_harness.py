"""Separate role login and file receipts on loopback. NEVER publish this harness."""
import argparse
import json
import os
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness import Harness, ROOT, CLIENT, ADMIN, ERRORS
from ledger import Ledger
from local_auth import LocalSessions
from receipt_store import ReceiptStore, MAX_FILE_BYTES, MIME
from bank_profile import load_profile


class OwnerHarness(Harness):
    def __init__(self, path, clock=None, *, recipient_profile=None):
        self.path, self.clock = str(path), clock or (lambda: int(time.time()))
        self.recipient = load_profile(recipient_profile) if recipient_profile is not None else None
        # Only an explicitly chosen LOCAL fixture path; never read env/config.
        if Path(path).is_symlink():
            raise ValueError('Local fixture database must not be a symlink')
        if not Path(path).exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        os.chmod(path, 0o600)
        ledger = Ledger(self.path, ADMIN)
        try:
            ReceiptStore(ledger)
            if not ledger.db.execute('SELECT 1 FROM search_guard_fixtures WHERE uid=?', (CLIENT,)).fetchone():
                ledger.seed_search_guard_fixture(CLIENT, enabled=False, epoch=9, sent_claims=13, uncertain_claims=2)
        finally:
            ledger.close()
        self.sessions = LocalSessions(self.path, self.clock, {'client': CLIENT, 'admin': ADMIN})

    def state(self, token):
        result = super().state(token)
        result['role'] = self.sessions.role(token)
        order = result['order']
        result['payment_instruction'] = self.recipient.instruction(
            order['id'], order['amount'], order['days']) if order and self.recipient else None
        ledger = Ledger(self.path, ADMIN)
        try:
            result['receipt_file'] = ReceiptStore(ledger).metadata(CLIENT, result['order']['id']) if result['order'] else None
        finally:
            ledger.close()
        return result

    def action(self, token, body):
        if isinstance(body, dict) and body.get('action') == 'receipt':
            raise ValueError('File receipt required')
        return super().action(token, body)

    def upload(self, token, order_id, mime, data):
        principal = self.sessions.resolve(token)
        if principal.uid != CLIENT:
            raise PermissionError('Client session required')
        ledger = Ledger(self.path, ADMIN)
        try:
            ReceiptStore(ledger).submit(principal.uid, order_id, mime, data, self.clock())
        finally:
            ledger.close()
        return self.state(token)

    def download(self, token, receipt_id):
        principal = self.sessions.resolve(token)
        ledger = Ledger(self.path, ADMIN)
        try:
            return ReceiptStore(ledger).download(principal.uid, receipt_id)
        finally:
            ledger.close()


LOCAL_ERRORS = {**ERRORS,
    'Receipt file too large or empty': 'Додай файл до 2 МБ.',
    'Receipt format mismatch': 'Дозволено PNG, JPEG або PDF із відповідним форматом.',
    'Receipt storage full': 'Тестове сховище квитанцій заповнене. Новий файл не збережено.',
    'File receipt required': 'Додай файл через форму квитанції.',
}


def owner_page():
    page = (ROOT / 'trial.html').read_text()
    login = '''<section id="owner-login" class="autodeal-trial ad-login">
<h2>Закритий локальний тест AUTODeal</h2>
<p>Введи одноразовий код для цієї ролі з локального файла власника. Не переказуй кошти.</p>
<label>Код входу<input id="owner-code" type="password" autocomplete="off" maxlength="43"></label>
<button id="owner-enter" type="button">Увійти</button><p id="owner-error" role="alert"></p>
</section><div id="owner-session" class="autodeal-trial" hidden>
<small id="owner-deadline"></small><button id="owner-rotate" type="button">Оновити ключ сесії</button>
<button id="owner-logout" type="button">Вийти</button><p id="owner-session-error" class="ad-error" role="alert"></p></div>'''
    page = page.replace('<div id="autodeal-payment-trial"', login+'<div hidden id="autodeal-payment-trial"')
    page = page.replace('<script src="/fixture-config.js"></script>', '')
    return page.replace('<script src="/connected.js"></script>',
                        '<script src="/owner-transport.js"></script><script src="/owner-login.js"></script>')


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, *args):
        try:
            super().process_request_thread(*args)
        finally:
            self.slots.release()


def server(harness, port=0, *, page_factory=owner_page):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass  # No credentials, bodies, filenames or receipt content in logs.

        def valid_host(self):
            return (self.headers.get('Sec-Fetch-Site') not in ('cross-site', 'same-site') and
                    self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}',
                                                 f'localhost:{self.server.server_port}'})

        def send(self, code, body, content_type='application/json; charset=utf-8', attachment=None):
            payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self' blob:; frame-ancestors 'none'; form-action 'none'" if not attachment else "sandbox; default-src 'none'")
            if attachment:
                self.send_header('Content-Disposition', f'attachment; filename="{attachment}"')
            self.end_headers()
            self.wfile.write(payload)

        def token(self):
            return self.headers.get('X-Local-Session')

        def denied(self, exc):
            expired = str(exc) in {'Local session required', 'Local session invalid or expired',
                                  'Invitation invalid or expired', 'Local clock moved backwards',
                                  'Fixture principal changed'}
            return self.send(401 if expired else 403, {'error': 'Код або сесія недійсні, або дія недоступна.'})

        def do_GET(self):
            if not self.valid_host():
                return self.send(403, {'error': 'Дозволено лише локальний тест.'})
            try:
                if self.path == '/api/state':
                    return self.send(200, harness.state(self.token()))
                match = re.fullmatch(r'/api/receipts/(RCPT-[a-f0-9]{32})', self.path)
                if match:
                    mime, data = harness.download(self.token(), match[1])
                    return self.send(200, data, 'application/octet-stream', 'receipt.'+MIME[mime][0])
                if self.path in ('/client', '/admin'):
                    return self.send(200, page_factory().encode(), 'text/html; charset=utf-8')
                files = {'/payment-ui.js': ('payment-ui.js', 'text/javascript; charset=utf-8'),
                         '/owner-transport.js': ('owner-transport.js', 'text/javascript; charset=utf-8'),
                         '/owner-login.js': ('owner-login.js', 'text/javascript; charset=utf-8'),
                         '/payment-ui.css': ('payment-ui.css', 'text/css; charset=utf-8'),
                         '/review-ui.js': ('review-ui.js', 'text/javascript; charset=utf-8')}
                if self.path not in files:
                    return self.send(404, {'error': 'Сторінку не знайдено.'})
                name, mime = files[self.path]
                return self.send(200, (ROOT / name).read_bytes(), mime)
            except PermissionError as exc:
                return self.denied(exc)
            except ValueError:
                return self.send(400, {'error': 'Квитанцію не знайдено.'})

        def do_POST(self):
            paths = {'/api/login', '/api/rotate', '/api/logout', '/api/action', '/api/receipt'}
            if not self.valid_host() or self.path not in paths:
                return self.send(403, {'error': 'Дозволено лише локальний тест.'})
            if (self.headers.get('X-AutoDeal-Local') != '1' or
                    self.headers.get('Origin') not in (None, 'http://'+self.headers.get('Host', '')) or
                    self.headers.get('Transfer-Encoding') is not None):
                return self.send(403, {'error': 'Запит відхилено.'})
            try:
                mime = self.headers.get('Content-Type')
                uploading = self.path == '/api/receipt'
                if (uploading and mime not in MIME) or (not uploading and mime != 'application/json'):
                    return self.send(415, {'error': 'Формат запиту не підтримується.'})
                # Authorize before reading receipt bytes, including expired tokens.
                if self.path != '/api/login':
                    principal = harness.sessions.resolve(self.token())
                    if uploading and principal.uid != CLIENT:
                        raise PermissionError('Client session required')
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                    return self.send(400, {'error': 'Потрібна точна довжина запиту.'})
                length = int(lengths[0])
                if not 0 < length <= (MAX_FILE_BYTES if uploading else 8192):
                    return self.send(413, {'error': 'Запит завеликий або порожній.'})
                raw = self.rfile.read(length)
                if len(raw) != length:
                    return self.send(400, {'error': 'Файл передано не повністю.'})
                if uploading:
                    return self.send(200, harness.upload(self.token(), self.headers.get('X-Order-ID'), mime, raw))
                body = json.loads(raw)
                if self.path == '/api/login':
                    if type(body) is not dict or set(body) != {'invitation'}:
                        raise ValueError('Only documented fields are accepted')
                    return self.send(200, harness.sessions.login(body['invitation']))
                if self.path in ('/api/rotate', '/api/logout'):
                    if body != {}:
                        raise ValueError('Only documented fields are accepted')
                    if self.path == '/api/rotate':
                        return self.send(200, harness.sessions.rotate(self.token()))
                    harness.sessions.revoke(self.token())
                    return self.send(200, {'ok': True})
                return self.send(200, harness.action(self.token(), body))
            except PermissionError as exc:
                return self.denied(exc)
            except (ValueError, TypeError, UnicodeError) as exc:
                return self.send(400, {'error': LOCAL_ERRORS.get(str(exc), 'Перевір дані тестової заявки.')})
            except TimeoutError:
                return self.send(408, {'error': 'Час передавання вичерпано.'})

    return BoundedServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description='Local owner payment review; no real transfers')
    parser.add_argument('--db', type=Path, help='Explicit LOCAL fixture DB, never production')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--recipient-profile', type=Path,
                        help='Private 0600 recipient JSON OUTSIDE repository; test view only')
    parser.add_argument('--issue-only', action='store_true', help='Create fresh one-use login codes, no server')
    args = parser.parse_args()
    if args.issue_only and args.db is None:
        parser.error('--issue-only requires an existing --db fixture')
    with tempfile.TemporaryDirectory(prefix='autodeal-owner-') as tmp:
        path = args.db or Path(tmp) / 'owner.sqlite'
        harness = OwnerHarness(path,recipient_profile=args.recipient_profile)
        invitations = {role: harness.sessions.issue_invitation(role) for role in ('client', 'admin')}
        # Private operator file, never an HTTP asset or a git artifact. Codes
        # are intentionally not printed. It survives issue-only until consumed.
        keys = path.with_name(path.name+'.local-invitations.json')
        fd = os.open(keys, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, json.dumps(invitations).encode())
        finally:
            os.close(fd)
        print(f'Одноразові коди входу (15 хв): {keys.resolve()}', flush=True)
        if args.issue_only:
            return
        app = server(harness, args.port)
        print(f'Лише локальний тест: http://127.0.0.1:{app.server_port}/client і /admin', flush=True)
        try:
            app.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            app.server_close()


if __name__ == '__main__':
    main()
