"""Backend regression is offline: external I/O must use fixture transports."""
import socket
import sys
import threading

import pytest


_CONNECT = socket.socket.connect
_CONNECT_EX = socket.socket.connect_ex
_SENDTO = socket.socket.sendto
_SENDMSG = getattr(socket.socket, "sendmsg", None)
_ATTEMPTS = []
_SOCKETPAIR = socket.socketpair
_PAIR = threading.local()


def _socketpair(*args, **kwargs):
    # Windows asyncio uses CPython's loopback socketpair, unlike Unix AF_UNIX.
    # Only the standard-library constructor may connect its internal pair.
    _PAIR.constructing = True
    try:
        return _SOCKETPAIR(*args, **kwargs)
    finally:
        _PAIR.constructing = False


def _deny(kind):
    _ATTEMPTS.append(kind)
    raise AssertionError("Offline test attempted " + kind + "; use a fixture transport")


def _connect(sock, address):
    if (getattr(_PAIR, 'constructing', False) and isinstance(address, tuple)
            and address[0] in ('127.0.0.1', '::1')):
        return _CONNECT(sock, address)
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _deny("network connect")
    return _CONNECT(sock, address)


def _connect_ex(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _deny("network connect")
    return _CONNECT_EX(sock, address)


def _sendto(sock, *args, **kwargs):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _deny("network datagram")
    return _SENDTO(sock, *args, **kwargs)


def _sendmsg(sock, *args, **kwargs):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        _deny("network datagram")
    return _SENDMSG(sock, *args, **kwargs)


def _getaddrinfo(*args, **kwargs):
    _deny("DNS")


def _install(setattr_fn):
    if sys.platform == 'win32':
        setattr_fn(socket, 'socketpair', _socketpair)
    setattr_fn(socket.socket, "connect", _connect)
    setattr_fn(socket.socket, "connect_ex", _connect_ex)
    setattr_fn(socket.socket, "sendto", _sendto)
    if _SENDMSG is not None:
        setattr_fn(socket.socket, "sendmsg", _sendmsg)
    setattr_fn(socket, "getaddrinfo", _getaddrinfo)


def pytest_sessionstart(session):
    # Fence collection/imports as well as individual tests. AF_UNIX socketpairs
    # remain available to asyncio/anyio; no Internet or DNS I/O is permitted.
    _install(setattr)


def pytest_configure(config):
    config.addinivalue_line("markers", "blocked_network_probe: deliberately verify the offline I/O fence")


def pytest_collection_finish(session):
    if _ATTEMPTS:
        raise pytest.UsageError("Network attempted during offline test collection")


@pytest.fixture(autouse=True)
def offline_network_guard(monkeypatch, request):
    _install(monkeypatch.setattr)
    _ATTEMPTS.clear()
    yield
    if request.node.get_closest_marker("blocked_network_probe") is None:
        assert not _ATTEMPTS, "Offline test swallowed blocked I/O; inject fixture transports: " + repr(_ATTEMPTS)

