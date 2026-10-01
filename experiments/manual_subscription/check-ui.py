"""Run real UI handlers against one temporary loopback SQLite harness."""
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from harness import Harness, server


def main():
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node is required for UI event checks')
    with tempfile.TemporaryDirectory(prefix='autodeal-ui-test-') as tmp:
        app = server(Harness(Path(tmp) / 'fixture.sqlite'))
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run([node, str(Path(__file__).with_name('test_payment_ui.cjs')),
                            f'http://127.0.0.1:{app.server_port}'], check=True, timeout=30)
        finally:
            app.shutdown()
            app.server_close()
            thread.join()


if __name__ == '__main__':
    main()
