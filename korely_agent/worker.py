"""Background work.

The jobs, and without the first one the product stores but never learns:

    facts      mine pending memories for typed facts
    vectors    compute the vectors a write could not get, with a bounded retry
               (services/vectors.py; nothing to do without a Google key)
    batch      import queued /v1/batch payloads
    webhooks   deliver the outbound queue
    purge      trim the worker's own records past their retention
    keys       warn about keys that are about to expire
    export     write the facts out as readable Markdown (off unless configured)

It runs inside the API process by default, so a working install is two
containers and one of them is Postgres. That is a deliberate trade: a separate
worker container is cleaner on paper and one more thing for a client to
operate, monitor and restart at three in the morning.

Set RUN_WORKER=false and run `python -m korely_agent.worker` separately when you
scale the API past one replica, or when you want the worker's failures isolated
from request serving. Running several is safe but wasteful: each drain claims
rows before working them, so replicas do not duplicate work, they contend for
it.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from typing import Callable

from .config import get_settings
from .db import get_sessionmaker
from .services.memory_core import log_safe

logger = logging.getLogger(__name__)

# Roughly the cadence of the service it replaces. Facts arrive a few seconds
# after a write, which is what `status: "processing"` is telling the caller.
FACTS_EVERY = 10
# A missed vector waits at least a minute before its retry (services/vectors.py),
# so a pass every thirty seconds is on time for every step of the ladder.
VECTORS_EVERY = 30
BATCH_EVERY = 30
WEBHOOKS_EVERY = 15
PURGE_EVERY = 3600
# Hourly is plenty: the thing it watches moves in days.
KEYS_EVERY = 3600


# What each job last did, for /health, like the export's record below: batch,
# webhooks, purge and keys failed on every tick, if they did, with /health
# saying "ok" (audit of 17 Oct 2026). Kept for every job; /health reports the
# ones nothing else reports (the facts and the vectors have the provider's
# and the embedder's state, the export its own). In-process: unknown after a
# restart, and with RUN_WORKER=false in the API.
_job_state: dict = {}
REPORTED_JOBS = ("batch", "webhooks", "purge", "keys")
#: The keys of a job's answer worth a log line when they are not zero.
NOTED = ("processed", "failed", "claimed", "delivered")


def _record(name: str, ok: bool, detail: str | None = None) -> None:
    from datetime import datetime, timezone

    _job_state[name] = {"ok": ok, "at": datetime.now(timezone.utc), "detail": detail}


def jobs_state() -> dict:
    """{job: working | failing (with the exception's class, never its
    message) | unknown} for the jobs in REPORTED_JOBS."""
    from datetime import datetime, timezone

    out = {}
    for name in REPORTED_JOBS:
        last = _job_state.get(name)
        if last is None:
            out[name] = {"state": "unknown", "detail": "this job has not run in this process yet"}
            continue
        ago = int((datetime.now(timezone.utc) - last["at"]).total_seconds())
        out[name] = ({"state": "working", "seconds_ago": ago} if last["ok"] else
                     {"state": "failing", "seconds_ago": ago, "detail": last["detail"]})
    return out


def _run(job: Callable[..., object], name: str, noted: tuple = NOTED) -> object:
    """One tick of one job, in its own session, swallowing its own failures.

    A job that raises must not take the loop down with it: the extraction drain
    failing on one malformed memory should not also stop webhook delivery for
    everybody. Errors are logged with the traceback and the next tick tries
    again.
    """
    db = get_sessionmaker()()
    try:
        result = job(db)
        _record(name, True)
        if isinstance(result, dict) and any(result.get(k) for k in noted):
            logger.info("[worker] %s: %s", name, result)
        return result
    except Exception as e:
        # The class and codes, not the message: a job's error can quote a
        # row, and /health answers anybody who asks.
        _record(name, False, log_safe.error(e))
        logger.error("[worker] %s failed: %s", name, log_safe.stack(e))
        try:
            db.rollback()
        except Exception:
            pass
        return None
    finally:
        db.close()


def _facts(db):
    from .core.llm import provider_configured
    from .services.facts.service import process_pending_memories

    # No writer (no key given at the first login, or removed): nothing can be
    # mined, so nothing is claimed. The memories wait 'pending' and the first
    # tick after a key is set mines them. Claimed, each found no provider and
    # was filed 'error', for good, seconds after its write (the upgrade check
    # of 2026-10-07).
    if not provider_configured():
        return {"processed": 0, "failed": 0, "claimed": 0, "no_writer": True}
    return process_pending_memories(db, limit=get_settings().drain_limit)


def _vectors(db):
    from .services.vectors import process_pending_vectors

    return process_pending_vectors(db, limit=get_settings().vector_drain_limit)


def _batch(db):
    from .services.batch import process_batch_jobs

    return process_batch_jobs(db, limit=get_settings().batch_drain_limit)


def _webhooks(db):
    from .services.webhooks import process_deliveries

    return process_deliveries(db, limit=get_settings().webhook_drain_limit)


# What the last export did, for /health: the in-process record, like the
# provider's (core.llm.provider_state). The export failed on every tick for
# anybody who followed the example and /health said "ok" (blind install,
# 2026-09-28).
_export_state: dict = {}


def export_state() -> dict:
    """The last export this process ran: working, failing (with why) or
    unknown (none yet here, as after a restart, or with RUN_WORKER=false)."""
    from datetime import datetime, timezone

    if not _export_state:
        return {"state": "unknown", "detail": "no export has run in this process yet"}
    ago = int((datetime.now(timezone.utc) - _export_state["at"]).total_seconds())
    if _export_state["ok"]:
        return {"state": "working", "seconds_ago": ago}
    return {"state": "failing", "seconds_ago": ago, "detail": _export_state["detail"]}


def _export(db):
    from datetime import datetime, timezone

    from .services.export_md import export_all

    where = get_settings().facts_export_dir
    try:
        out = export_all(db, where)
        _export_state.update(ok=True, at=datetime.now(timezone.utc), detail=None)
        return out
    except OSError as e:
        from .services.export_md import the_systems_reason

        # The class, errno and the system's words, never str(e): its path can
        # be an end user's file, named after them, and /health answers the
        # machine and its network with no key (audit of 18 Oct 2026).
        why = the_systems_reason(e)
        _export_state.update(ok=False, at=datetime.now(timezone.utc),
                             detail=f"FACTS_EXPORT_DIR={where} cannot be written ({why})")
        # The traceback alone said `Permission denied: '/data'` and nothing
        # about what to do; a tester read it and gave up on the feature. The
        # directory the compose file mounts is the answer in almost every case.
        raise OSError(
            f"FACTS_EXPORT_DIR={where} cannot be written by this process ({why}). "
            f"Under docker-compose.yml the directory to name is /data/facts, "
            f"which is ./facts on the host."
        ) from e


def _keys(db):
    from .services.keys import warn_about_expiring

    return warn_about_expiring(db)


def _purge(db):
    from .services.audit import purge_expired as purge_audit
    from .services.batch import purge_finished as purge_batches
    from .services.idempotency import purge_expired as purge_idempotency_keys
    from .services.llm_usage import purge_expired as purge_usage
    from .services.oauth import purge_expired as purge_oauth
    from .services.operators import purge_expired as purge_sessions
    from .services.rate_limit import purge_expired
    from .services.read_marks import purge_expired as purge_marks
    from .services.webhooks import purge_expired as purge_deliveries

    from .core import extensions

    s = get_settings()
    # Each record's trim on its own: one that fails is logged, rolled back and
    # reported, and the ones after it still run. A retention no clock could
    # hold stopped the purge at that record for every record after it, every
    # hour (review of piece 1 of agent notices, 8 Nov 2026). The first failure
    # is raised once every record had its turn, so /health says the job is
    # failing (`_run`).
    trims = (
        ("rate_limits", lambda: purge_expired(db)),
        ("audit", lambda: purge_audit(db, s.audit_retention_days)),
        ("llm_usage", lambda: purge_usage(db, s.llm_usage_retention_days)),
        ("sessions", lambda: purge_sessions(db)),
        ("webhook_deliveries", lambda: purge_deliveries(db, s.webhook_delivery_retention_days)),
        # The agent notices' read marks: which fact a named reader was handed,
        # kept READ_MARKS_RETENTION_DAYS after its last read (services/read_marks.py).
        ("read_marks", lambda: purge_marks(db, s.read_marks_retention_days)),
        # Finished /v1/batch jobs, payloads and all, after a fixed window
        # (services/batch.FINISHED_JOB_RETENTION says why it is not a setting).
        ("batches", lambda: purge_batches(db)),
        # The Idempotency-Keys of the writes past their 24 hours
        # (services/idempotency.LIFETIME, the expiry the docs publish).
        ("idempotency_keys", lambda: purge_idempotency_keys(db)),
        # OAuth's spent grants, the networks past their hour, the apps that
        # stopped renewing and the clients no live key holds (services/oauth.py).
        ("oauth", lambda: purge_oauth(db)),
        # What the hosted service keeps for a while (hosted/cli.cleanup);
        # nothing on an install of one's own.
        ("cloud", lambda: int(extensions.hooks.purge(db) or 0)),
    )
    n, failed = 0, []
    for name, trim in trims:
        try:
            n += trim()
        except Exception as e:
            logger.error("[worker] purge of %s failed: %s", name, log_safe.stack(e))
            try:
                db.rollback()
            except Exception:
                pass
            failed.append(e)
    if failed:
        raise failed[0]
    return {"processed": n} if n else {}


_JOBS = (
    ("facts", _facts, FACTS_EVERY),
    ("vectors", _vectors, VECTORS_EVERY),
    ("batch", _batch, BATCH_EVERY),
    ("webhooks", _webhooks, WEBHOOKS_EVERY),
    ("purge", _purge, PURGE_EVERY),
    ("keys", _keys, KEYS_EVERY),
    ("export", _export, None),      # cadence read from settings at start
)



# Each job's tick runs on a thread of the job's own (audit of 18 Oct
# 2026). With asyncio.to_thread the ticks took threads of the loop's default
# pool, min(32, CPUs + 4) threads: five on one CPU, six on two. The MCP tools
# wait on that same pool (core/threads.off_the_loop; their key check and
# OAuth's doors have had a pool of their own since the audit of 19 Oct 2026),
# and a tick
# holds its thread for as long as it runs (a facts tick with a backlog up to
# `drain_stale_minutes`, a batch import, a webhook round): five or six long
# ticks at once left the MCP and the token exchanges with no thread, queued
# for as long as the ticks. One
# loop runs one tick at a time, so one thread per job is all a job can use,
# and the default pool is left to the doors.
def _pool_of(name: str):
    """The job's own thread, one of the pools core/threads.py keeps."""
    from .core import threads

    return threads.pool(f"worker-{name}", 1)


async def _loop(name: str, job, every: int, stop: asyncio.Event, noted: tuple = NOTED) -> None:
    from .core.threads import on_its_pool

    # Blocking work goes to a thread: these jobs talk to Postgres and, for
    # extraction, to a model over the network. On the event loop they would
    # stall every request in the process for as long as a model call takes.
    while not stop.is_set():
        result = await on_its_pool(_pool_of(name), _run, job, name, noted)
        # A full batch with nothing failed means a backlog: the next tick
        # starts now rather than ten seconds from now (the facts drain's
        # `more`; services/facts/drain.py says when it is set).
        if isinstance(result, dict) and result.get("more"):
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except asyncio.TimeoutError:
            pass


async def run_forever(stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    s = get_settings()
    from .core.llm import provider_configured

    if not provider_configured():
        # A memory written meanwhile waits 'pending' (`_facts`); this said
        # 'error' and `manage reextract` (audit of 17 Oct 2026).
        logger.warning(
            "[worker] starting with no model key: a memory written before there "
            "is one waits 'pending', and the first pass after the key is in "
            "(dashboard, Settings, or GOOGLE_API_KEY) mines it."
        )
    jobs = [(n, j, e if e is not None else max(30, int(s.facts_export_every_seconds)), NOTED)
            for n, j, e in _JOBS]
    if not s.facts_export_dir:
        jobs = [x for x in jobs if x[0] != "export"]
    # The jobs of a part of the product the mode serves
    # (core/extensions.Part.jobs, mounted only on the Cloud).
    from .core import extensions

    jobs += [tuple(job) for part in extensions.mounted() for job in part.jobs()]
    logger.info("[worker] started: %s", ", ".join(f"{n} every {e}s" for n, _, e, _k in jobs))
    await asyncio.gather(*(_loop(n, j, e, stop, k) for n, j, e, k in jobs))


def give_back() -> None:
    """The worker's last act at a clean stop (the API's lifespan, `main`):
    the facts drain's claims back in the queue at once
    (services/facts/drain.give_back), not after `drain_stale_minutes`. A
    stop that never gets here (a kill, a crash) leaves them to the next
    drain of any process, which sees that this process has ended
    (services/holder.py). Never raises."""
    from .services.facts.drain import give_back as claims_back

    try:
        claims_back()
    except Exception as e:  # noqa: BLE001 - the next start gives them back
        logger.error("[worker] could not give the claims back at the stop: %s",
                     log_safe.stack(e))


def main() -> int:  # pragma: no cover
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s: %(message)s",
    )
    # The worker on its own runs the engine without create_app: it tells
    # memory_core which model makes the vectors itself (core/llm.py).
    from .core import extensions
    from .core.llm import pick_distance_thresholds
    from .services.vectors import refuse_vectors_of_another_model

    # What a part of the product does as a process starts, as create_app
    # does it (core/extensions.Part.at_start).
    for part in extensions.mounted():
        part.at_start(get_settings())
    pick_distance_thresholds()
    refuse_vectors_of_another_model()

    async def _go():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await run_forever(stop)

    asyncio.run(_go())
    give_back()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
