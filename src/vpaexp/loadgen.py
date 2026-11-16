"""Open-loop load generator (spec section 5)."""

from __future__ import annotations

import asyncio
import csv
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import aiohttp
import numpy as np
from numpy.typing import NDArray

from vpaexp.trace import Floats

RESULT_TOLERANCE = 1e-9  # the tolerance SeBS's validate_output uses
HEADER = ["scheduled", "sent", "completed", "status"]


@dataclass(frozen=True)
class ClientLog:
    """One row per request. Times are epoch seconds; status is ok, error or timeout."""

    scheduled: Floats
    sent: Floats
    completed: Floats
    status: NDArray[np.str_]


def read_client_log(path: Path) -> ClientLog:
    with open(path) as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda r: float(r["scheduled"]))

    def col(name: str) -> Floats:
        return np.array([float(r[name]) for r in rows], dtype=np.float64)

    return ClientLog(col("scheduled"), col("sent"), col("completed"), np.array([r["status"] for r in rows], dtype=str))


def run_replay(
    url: str,
    schedule: Floats,
    payload: dict[str, Any],
    timeout_s: float,
    log_path: Path,
    expected_result: float | None = None,
) -> float:
    """Send one request per schedule entry (seconds from now). Returns the epoch time of replay start."""
    return asyncio.run(_replay(url, schedule, payload, timeout_s, log_path, expected_result))


async def _replay(
    url: str,
    schedule: Floats,
    payload: dict[str, Any],
    timeout_s: float,
    log_path: Path,
    expected_result: float | None,
) -> float:
    loop = asyncio.get_running_loop()
    epoch0, mono0 = time.time(), loop.time()

    def epoch(mono: float) -> float:
        return epoch0 + (mono - mono0)

    timeout = aiohttp.ClientTimeout(total=timeout_s)
    connector = aiohttp.TCPConnector(limit=0, force_close=True)
    tasks: set[asyncio.Task[None]] = set()
    with open(log_path, "w", newline="") as f:
        out = csv.writer(f)
        out.writerow(HEADER)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            for offset in np.sort(schedule).tolist():
                delay = mono0 + offset - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                task = asyncio.create_task(
                    _one(session, url, payload, expected_result, epoch(mono0 + offset), epoch, out, f)
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            if tasks:
                await asyncio.gather(*tasks)
    return epoch0


async def _one(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict[str, Any],
    expected_result: float | None,
    scheduled: float,
    epoch: Any,
    out: Any,
    f: TextIO,
) -> None:
    loop = asyncio.get_running_loop()
    sent = loop.time()
    try:
        async with session.post(url, json=payload) as resp:
            body = await resp.json(content_type=None)
            ok = resp.status == 200 and _result_ok(body, expected_result)
            status = "ok" if ok else "error"
    except TimeoutError:
        status = "timeout"
    except (aiohttp.ClientError, ValueError, OSError):
        status = "error"
    out.writerow([f"{scheduled:.6f}", f"{epoch(sent):.6f}", f"{epoch(loop.time()):.6f}", status])
    f.flush()


def _result_ok(body: Any, expected: float | None) -> bool:
    if expected is None:
        return True
    result = body.get("result") if isinstance(body, dict) else None
    return isinstance(result, (int, float)) and abs(float(result) - expected) <= RESULT_TOLERANCE
