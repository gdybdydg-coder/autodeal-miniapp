"""Fence saved-real imports and test execution before touching any module."""
import socket

def deny(*a,**kw):raise AssertionError('Saved-real replay cannot access the network')

def pytest_sessionstart(session):
    socket.socket.connect=deny
    socket.socket.connect_ex=deny
    socket.getaddrinfo=deny
    socket.socket.sendto=deny
    if hasattr(socket.socket,'sendmsg'):socket.socket.sendmsg=deny
