"""Fence network/DNS BEFORE collecting both old and new isolated tests."""
import socket,sys
from unittest.mock import patch
import pytest

def denied(*args,**kwargs):raise AssertionError('Research verification forbids external network')
def main():
    with patch.object(socket,'getaddrinfo',denied),patch.object(socket.socket,'connect',denied),patch.object(socket.socket,'connect_ex',denied),patch.object(socket.socket,'sendto',denied):
        return pytest.main(['-q','experiments/olx_offline','experiments/olx_research'])
if __name__=='__main__':sys.exit(main())
