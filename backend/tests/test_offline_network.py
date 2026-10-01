"""The test guard prevents packets/DNS before any external operation."""
import socket

import pytest


@pytest.mark.parametrize("method", ["connect", "connect_ex", "sendto"])
@pytest.mark.blocked_network_probe
def test_internet_operations_require_fixture_transport(method):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(AssertionError, match="Offline test attempted network"):
            if method == "sendto":
                sock.sendto(b"fixture", ("192.0.2.1", 443))
            else:
                getattr(sock, method)(("192.0.2.1", 443))


@pytest.mark.blocked_network_probe
def test_dns_is_blocked_before_resolution():
    with pytest.raises(AssertionError, match="Offline test attempted DNS"):
        socket.getaddrinfo("fixture.invalid", 443)


def test_local_socketpair_for_asyncio_remains_available():
    left, right = socket.socketpair()
    try:
        left.send(b"fixture")
        assert right.recv(7) == b"fixture"
    finally:
        left.close()
        right.close()
