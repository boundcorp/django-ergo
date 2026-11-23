import logging

import pytest
from uvicorn.logging import AccessFormatter

from ergonaut.access_logging import OAuthCallbackQueryFilter, install_access_log_filter


@pytest.mark.parametrize(
    "target",
    [
        "/oauth/google/callback/?code=synthetic_code&state=synthetic_state",
        "/oauth/google/callback/?state=synthetic_state?code=synthetic_code",
        "/oauth/google/callback/?code=synthetic_code#state=synthetic_state",
        "/oauth/google/callback/?%zz=synthetic_code&state=synthetic_state",
        "/oauth/google/%63allback/?code=synthetic_code&state=synthetic_state",
    ],
)
def test_callback_redacts_all_query_in_uvicorn_formatter(target):
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:123", "GET", target, "1.1", 400),
        None,
    )
    assert OAuthCallbackQueryFilter().filter(record)
    rendered = AccessFormatter('%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s').format(record)
    assert "synthetic_code" not in rendered
    assert "synthetic_state" not in rendered
    assert "?" not in rendered
    assert "GET /oauth/google/" in rendered
    assert "400" in rendered


def test_other_paths_keep_query():
    target = "/other/?code=synthetic_code&state=synthetic_state"
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "%s %s %s %s %s", ("client", "GET", target, "1.1", 200), None
    )
    OAuthCallbackQueryFilter().filter(record)
    assert target in record.getMessage()


def test_filter_installed_once():
    logger = logging.getLogger("uvicorn.access")
    old_filters = logger.filters[:]
    try:
        install_access_log_filter()
        install_access_log_filter()
        assert sum(isinstance(item, OAuthCallbackQueryFilter) for item in logger.filters) == 1
    finally:
        logger.filters[:] = old_filters


@pytest.mark.asyncio
async def test_running_uvicorn_access_log(caplog):
    import asyncio
    import socket
    from urllib.request import urlopen

    import uvicorn

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    logger = logging.getLogger("uvicorn.access")
    old_filters = logger.filters[:]
    install_access_log_filter()
    server = uvicorn.Server(uvicorn.Config(app, http="h11", log_config=None, lifespan="off"))
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        for path in (
            "/oauth/google/callback/?code=synthetic_code&state=synthetic_state",
            "/control/?visible=synthetic_control",
        ):
            await asyncio.to_thread(lambda: urlopen(f"http://127.0.0.1:{port}{path}").close())
        messages = [record.getMessage() for record in caplog.records if record.name == "uvicorn.access"]
        assert len(messages) == 2
        assert 'GET /oauth/google/callback/ HTTP/1.1" 204' in messages[0]
        assert "synthetic_code" not in messages[0] and "synthetic_state" not in messages[0]
        assert "/control/?visible=synthetic_control" in messages[1]
    finally:
        server.should_exit = True
        await task
        listener.close()
        logger.filters[:] = old_filters
