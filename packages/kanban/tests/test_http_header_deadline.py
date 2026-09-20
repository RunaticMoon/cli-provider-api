"""Whole-response bounds also cover parsing status and chunk headers."""
import contextlib
import socket
import threading
import time

import pytest

from cli_provider_kanban.wrapper_client import WrapperClient, WrapperTransportError


@pytest.mark.parametrize('prefix', [b'HTTP/1.1 200 ', b'HTTP/1.1 200 OK\r\nX-Drip: ', b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'])
def test_deadline_bounds_incomplete_http_framing(prefix):
    stop = threading.Event()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]

    def serve():
        with contextlib.suppress(OSError):
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(5)
                request = b''
                while b'\r\n\r\n' not in request:
                    request += conn.recv(8192)
                conn.sendall(prefix)
                # Keep every read active, but never finish this framing line.
                until = time.monotonic() + 4
                while not stop.wait(.04) and time.monotonic() < until:
                    conn.sendall(b'a')

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        client = WrapperClient(f'http://127.0.0.1:{port}', timeout_seconds=.4)
        started = time.monotonic()
        with pytest.raises(WrapperTransportError):
            client.get_run('run_deadline')
        assert time.monotonic() - started < 2, 'framing reads renewed the socket timeout'
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=6)
        assert not thread.is_alive()
