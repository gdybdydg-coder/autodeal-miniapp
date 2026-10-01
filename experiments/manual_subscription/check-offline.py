"""Run the complete isolated prototype suite; outbound TCP/DNS is fenced."""
import socket
import unittest
from pathlib import Path

ALLOWED = {'127.0.0.1', '::1', 'localhost'}


def local_only(value):
    host = value[0] if isinstance(value, tuple) else value
    if host not in ALLOWED:
        raise AssertionError('Prototype tests forbid non-loopback TCP/DNS')


def main():
    originals = {}
    for name in ('connect', 'connect_ex'):
        original = getattr(socket.socket, name)
        originals[name] = original
        def fenced_connect(self, address, _original=original):
            local_only(address)
            return _original(self, address)
        setattr(socket.socket, name, fenced_connect)
    for name in ('getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr'):
        original = getattr(socket, name)
        originals[name] = original
        def fenced_lookup(host, *args, _original=original, **kwargs):
            local_only(host)
            return _original(host, *args, **kwargs)
        setattr(socket, name, fenced_lookup)
    try:
        suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    finally:
        for name, original in originals.items():
            setattr(socket.socket if name in ('connect', 'connect_ex') else socket, name, original)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print('Complete prototype regression passed with non-loopback TCP/DNS fenced')


if __name__ == '__main__':
    main()
