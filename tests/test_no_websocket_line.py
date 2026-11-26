"""The server as the image starts it writes no line with a request's path
on a WebSocket handshake. The app has no WebSocket route, and uvicorn with
a WebSocket library installed answered the handshake itself, writing
`"WebSocket /v1/users/maria%40x.com/memories?query=..." 403` on
uvicorn.error, a line no filter of the app shapes (review of 27 Oct 2026,
after the screen's routes logged any path raw). The shipped Caddy passes
an Upgrade header on. The command is read from the Dockerfile's CMD, the
one place the server is started, and run as it is."""
import json
import logging
import re
import socket
import threading
import time
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"


def _cmd() -> list:
    text = DOCKERFILE.read_text().replace("\\\n", " ")
    found = re.findall(r"^CMD (\[.*\])\s*$", text, re.M)
    assert len(found) == 1, "one CMD starts the server"
    return json.loads(found[0])


def _options() -> dict:
    """The CMD's options as uvicorn's own command line reads them."""
    from uvicorn.main import main

    cmd = _cmd()
    assert cmd[0] == "uvicorn"
    return main.make_context("uvicorn", cmd[1:]).params


def test_the_image_starts_the_server_without_websockets():
    assert _options()["ws"] == "none"


def test_a_handshake_leaves_no_path_in_the_log():
    import uvicorn
    from fastapi import FastAPI

    from korely_agent.main import _quiet_access_log

    _quiet_access_log()
    opts = _options()
    config = uvicorn.Config(FastAPI(), ws=opts["ws"], http=opts["http"],
                            proxy_headers=opts["proxy_headers"], lifespan="off",
                            # uvicorn's own log config, which the image runs,
                            # writes uvicorn.error at INFO
                            log_config=None, log_level=opts["log_level"] or "info")
    server = uvicorn.Server(config)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    lines = []

    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())
            records.append(record)

    handler = Keep(level=logging.DEBUG)
    # On each of uvicorn's two loggers: its own log config, which another
    # test may have run, stops the access logger's propagation.
    loggers = [logging.getLogger(n) for n in ("uvicorn.error", "uvicorn.access")]
    disabled = [lg.disabled for lg in loggers]
    for lg in loggers:
        lg.disabled = False
        lg.addHandler(handler)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            c.sendall(b"GET /v1/users/maria%40x.com/memories?query=diagnosis HTTP/1.1\r\n"
                      b"Host: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                      b"Sec-WebSocket-Version: 13\r\n\r\n")
            answer = c.recv(4096)
        assert answer.startswith(b"HTTP/1.1 ")
        time.sleep(0.2)
    finally:
        server.should_exit = True
        thread.join(10)
        for lg, was in zip(loggers, disabled):
            lg.removeHandler(handler)
            lg.disabled = was
    assert lines, "the server logged the request"
    leaked = [line for line in lines if "maria" in line or "diagnosis" in line]
    assert not leaked, leaked
    # No warning an operator would read as a broken install ("Unsupported
    # upgrade request.", "No supported WebSocket library detected"): the
    # request is an HTTP one, written once by the access line.
    warned = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
    assert not warned, warned
    assert len([r for r in records if r.name == "uvicorn.access"]) == 1, lines
