"""Owner review on 127.0.0.1 only. Synthetic sessions/receipts; no outgoing APIs."""
import argparse
import json
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from adapter import Adapter, FixtureSessions
from ledger import Ledger

ROOT = Path(__file__).parent
CLIENT = 111
ADMIN = 777292211


class Harness:
    def __init__(self, path, clock=None):
        self.path = str(path)
        self.clock = clock or (lambda: int(time.time()))
        self.sessions = FixtureSessions()
        self.tokens = {'client': self.sessions.issue(CLIENT), 'admin': self.sessions.issue(ADMIN)}
        ledger = Ledger(self.path, ADMIN)
        if not ledger.db.execute('SELECT 1 FROM search_guard_fixtures WHERE uid=?', (CLIENT,)).fetchone():
            ledger.seed_search_guard_fixture(CLIENT, enabled=False, epoch=9, sent_claims=13, uncertain_claims=2)
        ledger.close()

    def state(self, token):
        principal = self.sessions.resolve(token)
        if principal.uid not in (CLIENT, ADMIN):
            raise PermissionError('Fixture participant required')
        ledger = Ledger(self.path, ADMIN)
        try:
            row = ledger.db.execute(
                'SELECT id,uid,amount,days,created FROM orders WHERE uid=? ORDER BY rowid DESC LIMIT 1',
                (CLIENT,),
            ).fetchone()
            order = None
            if row:
                oid, owner, amount, days, created = row
                status = ledger.order_status(owner, oid)
                order = {'id': oid, 'amount': amount, 'days': days, 'created': created, **status}
            member = ledger.db.execute('SELECT expires_at FROM memberships WHERE uid=?', (CLIENT,)).fetchone()
            return {'order': order, 'amount': 249, 'days': 30,
                    'membership': {'active': ledger.active(CLIENT, self.clock()),
                                   'expires_at': member[0] if member else None},
                    'search_enabled': ledger.entitlement(CLIENT, self.clock())['search_enabled']}
        finally:
            ledger.close()

    def action(self, token, body):
        if (type(body) is not dict or set(body) != {'action', 'order_id', 'data'} or
                type(body['action']) is not str or type(body['data']) is not dict or
                (body['order_id'] is not None and type(body['order_id']) is not str)):
            raise ValueError('Only documented fields are accepted')
        ledger = Ledger(self.path, ADMIN)
        try:
            api = Adapter(ledger, self.sessions)
            action, oid, data, now = body['action'], body['order_id'], body['data'], self.clock()
            if action == 'create':
                if oid is not None:
                    raise ValueError('No order ID accepted on create')
                # The owner review UI has both roles; a client session still
                # cannot use admin methods or set uid, tariff, clock or expiry.
                if self.sessions.resolve(token).uid != CLIENT:
                    raise PermissionError('Client session required')
                api.create(token, data, now)
            elif action == 'receipt':
                api.submit(token, oid, data, now)
            elif action == 'clarify':
                api.clarify(token, oid, data, now)
            elif action == 'approve':
                api.approve(token, oid, data, now)
            elif action == 'reject':
                api.reject(token, oid, data, now)
            else:
                raise ValueError('Unknown fixture action')
        finally:
            ledger.close()
        return self.state(token)


ERRORS = {
    'Payment amount mismatch': 'Сума не збігається з тестовим тарифом.',
    'Actual bank verification required': 'Познач тестову звірку надходження.',
    'Payment already used': 'Цей тестовий платіж уже зараховано.',
    'Receipt review required': 'Спочатку надішли тестову квитанцію.',
    'Order already under review or closed': 'Заявка вже перевіряється або закрита.',
    'Clarification text required (max 500 characters)': 'Вкажи уточнення до 500 символів.',
}


def server(harness, port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log fixture session headers or bodies.

        def valid_host(self):
            return self.headers.get('Sec-Fetch-Site') not in ('cross-site', 'same-site') and self.headers.get('Host') in {
                f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

        def send(self, code, body, content_type='application/json; charset=utf-8'):
            payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not self.valid_host():
                return self.send(403, {'error': 'Дозволено лише локальний тест.'})
            try:
                if self.path == '/api/state':
                    return self.send(200, harness.state(self.headers.get('X-Fixture-Session')))
                files = {'/': ('trial.html', 'text/html; charset=utf-8'),
                         '/payment-ui.js': ('payment-ui.js', 'text/javascript; charset=utf-8'),
                         '/connected.js': ('connected.js', 'text/javascript; charset=utf-8'),
                         '/payment-ui.css': ('payment-ui.css', 'text/css; charset=utf-8')}
                if self.path == '/fixture-config.js':
                    data = 'window.AutoDealFixtureConfig=' + json.dumps(harness.tokens) + ';'
                    return self.send(200, data.encode(), 'text/javascript; charset=utf-8')
                if self.path not in files:
                    return self.send(404, {'error': 'Сторінку не знайдено.'})
                name, mime = files[self.path]
                return self.send(200, (ROOT / name).read_bytes(), mime)
            except PermissionError:
                return self.send(403, {'error': 'Тестова сесія недійсна.'})

        def do_POST(self):
            if not self.valid_host() or self.path != '/api/action':
                return self.send(403, {'error': 'Дозволено лише локальний тест.'})
            origin = self.headers.get('Origin')
            allowed_origin = {'http://' + self.headers.get('Host', '')}
            if (self.headers.get('X-AutoDeal-Fixture') != '1' or
                    (origin is not None and origin not in allowed_origin) or
                    self.headers.get('Content-Type') != 'application/json'):
                return self.send(403, {'error': 'Запит тестового екрана відхилено.'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192:
                    return self.send(413, {'error': 'Завеликий тестовий запит.'})
                body = json.loads(self.rfile.read(length))
                return self.send(200, harness.action(self.headers.get('X-Fixture-Session'), body))
            except PermissionError:
                return self.send(403, {'error': 'Ця дія недоступна в обраній ролі.'})
            except (ValueError, TypeError, UnicodeError) as exc:
                return self.send(400, {'error': ERRORS.get(str(exc), 'Перевір дані тестової заявки.')})

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description='AUTODeal loopback fixture review; no payments')
    parser.add_argument('--db', type=Path, help='Explicit offline database path for restart checks')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='autodeal-fixture-') as tmp:
        path = args.db or Path(tmp) / 'fixture.sqlite'
        app = server(Harness(path), args.port)
        print(f'Тест без переказів: http://127.0.0.1:{app.server_port}', flush=True)
        try:
            app.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            app.server_close()


if __name__ == '__main__':
    main()
