import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
PYTHON_MODEL = (ROOT / "ledger.py", ROOT / "adapter.py")
UI_MODEL = (ROOT / "workflow.js", ROOT / "demo.html")
FORBIDDEN_IMPORTS = {
    "boto3", "botocore", "http", "httpx", "psycopg", "redis", "requests",
    "socket", "telegram", "urllib", "websockets",
}
NETWORK_MARKERS = (
    "fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon",
    "http://", "https://", "<form action=",
)
PERSISTENT_BROWSER_STATE = ("localStorage", "sessionStorage", "indexedDB", "document.cookie")
PAYMENT_SECRET_MARKERS = ("card_number", "iban", "recipient_account", "bank_api_key")


class SafetyReview(unittest.TestCase):
    def test_python_model_has_no_network_or_external_service_imports(self):
        imports = set()
        for path in PYTHON_MODEL:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
        self.assertFalse(imports & FORBIDDEN_IMPORTS, imports & FORBIDDEN_IMPORTS)

    def test_local_ui_has_no_transport_or_persistent_browser_state(self):
        text = "\n".join(path.read_text(encoding="utf-8") for path in UI_MODEL)
        self.assertFalse([marker for marker in NETWORK_MARKERS if marker in text])
        self.assertFalse([marker for marker in PERSISTENT_BROWSER_STATE if marker in text])

    def test_executable_prototype_contains_no_card_or_recipient_credentials(self):
        paths = PYTHON_MODEL + UI_MODEL
        text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
        self.assertFalse([marker for marker in PAYMENT_SECRET_MARKERS if marker in text.lower()])
        self.assertIsNone(re.search(r"(?<!\d)\d{13,19}(?!\d)", text))


if __name__ == "__main__":
    unittest.main()
