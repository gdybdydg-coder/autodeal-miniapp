"""Backend regression is offline: external I/O must use fixture transports."""
import socket

import pytest


_CONNECT = socket.socket.connect
_CONNECT_EX = socket.socket.connect_ex
_SENDTO = socket.socket.sendto
_SENDMSG = getattr(socket.socket, "sendmsg", None)
_ATTEMPTS = []


def _deny(kind):
    _ATTEMPTS.append(kind)
    raise AssertionError("Offline test attempted " + kind + "; use a fixture transport")


def _connect(sock, address):
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
