from __future__ import annotations

import os
import socket
import sys

import pytest

from envelope.execution.ports import listening_ports, port_reachable

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="exercises the Linux port table")


def test_listening_ports_finds_own_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        ports = listening_ports(frozenset({os.getpid()}))
        assert ports is not None
        assert port in ports


def test_listening_ports_ignores_other_pids() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        # pid 1 (init/systemd) does not own this socket.
        ports = listening_ports(frozenset({1}))
        assert ports is not None
        assert port not in ports


def test_listening_ports_empty_for_no_pids() -> None:
    assert listening_ports(frozenset()) == frozenset()


def test_port_reachable_true_for_a_listening_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert port_reachable(port) is True


def test_port_reachable_false_for_a_closed_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    # the socket is now closed; nothing listens on `port` anymore
    assert port_reachable(port) is False
