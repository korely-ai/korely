"""The worker's ticks never take the threads the MCP and OAuth wait on.

Audit of 18 Oct 2026: worker._loop ran each tick with asyncio.to_thread,
on the loop's default pool, and core/threads.off_the_loop (every MCP tool,
the MCP's key check, OAuth's doors) waits on the same pool: min(32, CPUs +
4) threads, five on one CPU and six on two. Five or six long ticks at once
(a facts tick with a backlog, a batch import, a webhook round) left the MCP
with no thread, queued for as long as the ticks. The real `_loop` and the
real `off_the_loop` here, the jobs replaced by stand-ins that block.
"""
from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

from korely_agent import worker
from korely_agent.core import threads

HOLD = 1.5


class _NoSession:
    def close(self):
        pass

    def rollback(self):
        pass


def _blocking(_db):
    time.sleep(HOLD)
    return {}


def test_a_door_gets_a_thread_while_every_job_is_in_a_long_tick(monkeypatch):
    monkeypatch.setattr(worker, "get_sessionmaker", lambda: _NoSession)
    names = [n for n, _j, _e in worker._JOBS]

    async def scenario():
        # The default pool of a one-CPU host, smaller than the jobs.
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=5))
        stop = asyncio.Event()
        loops = [asyncio.create_task(worker._loop(n, _blocking, 3600, stop)) for n in names]
        await asyncio.sleep(0.2)  # every job is inside its tick
        t0 = time.monotonic()
        await threads.off_the_loop(time.time)
        waited = time.monotonic() - t0
        stop.set()
        await asyncio.gather(*loops)
        return waited

    waited = asyncio.run(scenario())
    assert waited < 0.3, f"a trivial off_the_loop call waited {waited:.2f} s behind the worker"


def test_one_job_runs_one_tick_at_a_time_on_its_own_thread(monkeypatch):
    import threading

    monkeypatch.setattr(worker, "get_sessionmaker", lambda: _NoSession)
    seen = []

    def job(_db):
        seen.append(threading.current_thread().name)
        return {"more": True} if len(seen) < 3 else {}

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(worker._loop("purge", job, 3600, stop))
        while len(seen) < 3:
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    assert len(set(seen)) == 1 and seen[0].startswith("worker-purge"), seen
