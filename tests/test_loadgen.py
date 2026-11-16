import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest
from aiohttp import web

from vpaexp.loadgen import ClientLog, read_client_log, run_replay

Handler = Callable[[web.Request], "asyncio.Future[web.Response] | object"]


@contextmanager
def function_server(delay_s: float = 0.0, status: int = 200, result: float = 1.0) -> Iterator[str]:
    """A stand-in for the function pod, on a real local socket."""
    import threading

    async def handle(request: web.Request) -> web.Response:
        assert await request.json() == {"size": 3, "seed": 42}
        await asyncio.sleep(delay_s)
        return web.json_response({"result": result}, status=status)

    loop = asyncio.new_event_loop()
    app = web.Application()
    app.router.add_post("/", handle)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", 0)
    loop.run_until_complete(site.start())
    port = runner.addresses[0][1]
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join()


def replay(url: str, schedule: list[float], tmp_path: Path, timeout_s: float = 2.0, expected: float | None = 1.0) -> ClientLog:
    log = tmp_path / "client.csv"
    run_replay(url, np.array(schedule), {"size": 3, "seed": 42}, timeout_s, log, expected_result=expected)
    return read_client_log(log)


def test_requests_go_out_on_schedule_even_while_earlier_ones_are_in_flight(tmp_path: Path) -> None:
    with function_server(delay_s=0.5) as url:
        before = time.time()
        log = replay(url, [0.0, 0.1, 0.2], tmp_path)
        elapsed = time.time() - before
    assert elapsed < 1.2  # a closed loop would take 1.5s
    assert log.status.tolist() == ["ok", "ok", "ok"]
    assert np.diff(log.scheduled) == pytest.approx([0.1, 0.1], abs=1e-4)
    assert log.sent - log.scheduled == pytest.approx([0, 0, 0], abs=0.05)
    assert log.completed - log.sent == pytest.approx([0.5, 0.5, 0.5], abs=0.1)
    assert before <= log.scheduled[0] <= before + 0.5  # timestamps are wall-clock epoch seconds


def test_a_request_slower_than_the_timeout_is_recorded_as_a_timeout(tmp_path: Path) -> None:
    with function_server(delay_s=1.0) as url:
        log = replay(url, [0.0], tmp_path, timeout_s=0.2)
    assert log.status.tolist() == ["timeout"]
    assert log.completed - log.sent == pytest.approx([0.2], abs=0.1)


def test_a_server_error_is_recorded_as_an_error(tmp_path: Path) -> None:
    with function_server(status=500) as url:
        log = replay(url, [0.0], tmp_path)
    assert log.status.tolist() == ["error"]


def test_a_refused_connection_is_recorded_as_an_error(tmp_path: Path) -> None:
    log = replay("http://127.0.0.1:9/", [0.0], tmp_path)
    assert log.status.tolist() == ["error"]


def test_a_response_with_the_wrong_result_is_an_error(tmp_path: Path) -> None:
    with function_server(result=0.5) as url:
        log = replay(url, [0.0], tmp_path, expected=1.0)
    assert log.status.tolist() == ["error"]
