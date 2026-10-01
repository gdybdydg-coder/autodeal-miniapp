"""Same-process network namespace: actual owner UI -> local auth/files/SQLite."""
import base64
import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from owner_harness import OwnerHarness, server
from test_receipt_store import PNG
from test_bank_profile import private_fixture, fixture_data


def main():
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node is required for UI event checks')
    with tempfile.TemporaryDirectory(prefix='autodeal-owner-ui-') as tmp:
        profile = private_fixture(Path(tmp) / 'recipient.private.json')
        harness = OwnerHarness(Path(tmp) / 'fixture.sqlite',recipient_profile=profile)
        invitations = {role:harness.sessions.issue_invitation(role) for role in ('client','admin')}
        app = server(harness)
        thread = threading.Thread(target=app.serve_forever,daemon=True)
        thread.start()
        try:
            config = {'base':f'http://127.0.0.1:{app.server_port}', 'invitations':invitations,
                      'refreshInvitation':harness.sessions.issue_invitation('client'),
                      'adminRefreshInvitation':harness.sessions.issue_invitation('admin'),
                      'expectedRecipient':fixture_data(),
                      'png':base64.b64encode(PNG).decode()}
            # One-use local fixture codes go via stdin, never argv/stdout/logs.
            subprocess.run([node,str(Path(__file__).with_name('test_owner_ui.cjs'))],
                           input=json.dumps(config),text=True,check=True,timeout=30)
        finally:
            app.shutdown()
            app.server_close()
            thread.join()


if __name__ == '__main__':
    main()
