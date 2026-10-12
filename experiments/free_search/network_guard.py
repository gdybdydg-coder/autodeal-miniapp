"""Offline experiment network boundary, never a production integration."""
from contextlib import contextmanager
from dataclasses import dataclass
import os
import socket
from unittest.mock import patch
from urllib.parse import urlsplit

CREDENTIAL_NAMES = frozenset({'DATABASE_URL','TELEGRAM_BOT_TOKEN','BOT_TOKEN','AUTO_RIA_API_KEY','AUTORIA_API_KEY','RIA_API_KEY','OPENAI_API_KEY','RENDER_API_KEY'})

class NetworkDenied(RuntimeError):
    pass

@dataclass
class OutboundGuard:
    attempted: int = 0
    blocked: int = 0
    paid_blocked: int = 0
    successful_external_calls: int = 0
    paid_calls: int = 0

    def check_url(self, url):
        self.attempted += 1
        self.blocked += 1
        host = (urlsplit(url).hostname or '').lower()
        if host == 'developers.ria.com' or host.endswith('.developers.ria.com') or host == 'api.auto.ria.com':
            self.paid_blocked += 1
        raise NetworkDenied('offline_outbound_denied')

    def _socket_denied(self, *args, **kwargs):
        self.attempted += 1
        self.blocked += 1
        raise NetworkDenied('offline_socket_denied')

    @contextmanager
    def isolated(self, *, reject_credentials=True):
        if reject_credentials and any(os.environ.get(k) for k in CREDENTIAL_NAMES):
            raise NetworkDenied('production_configuration_present')
        with patch.object(socket,'socket',self._socket_denied), patch.object(socket,'create_connection',self._socket_denied), patch.object(socket,'getaddrinfo',self._socket_denied):
            yield self

    def summary(self):
        return dict(vars(self))
