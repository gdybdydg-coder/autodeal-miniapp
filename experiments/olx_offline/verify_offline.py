"""Run all experiment cases with Internet/DNS fenced before test collection."""
import socket
import unittest
from unittest.mock import patch


def forbidden(*args, **kwargs):
    raise AssertionError('Offline verification attempted external I/O')


def main():
    with patch.object(socket, 'getaddrinfo', forbidden), \
         patch.object(socket.socket, 'connect', forbidden), \
         patch.object(socket.socket, 'connect_ex', forbidden), \
         patch.object(socket.socket, 'sendto', forbidden):
        suite = unittest.defaultTestLoader.discover('experiments/olx_offline')
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
