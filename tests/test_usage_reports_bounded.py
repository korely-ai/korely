"""The usage reports hold their size and their clock (audit of 27 Oct 2026).

- GET /admin/usage listed every agent id the writes had named, with a query
  more for each one;
- GET /admin/requests/daily bucketed by the database session's date and
  filled the series with UTC days.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import event, text

from korely_agent.db.models import AgentLlmUsage, AuditEvent
from korely_agent.services import audit, usage_report


def _used(db, tenant, agent, tout, model="m1"):
    db.add(AgentLlmUsage(id=uuid.uuid4(), tenant_id=tenant.id, stage="extraction",
                         provider="gemini", model=model, prompt_tokens=10,
                         completion_tokens=tout, agent_id=agent, cached_tokens=1,
                         ts=datetime.now(timezone.utc)))


def _counted(db, fn):
    seen = []

    def count(conn, cursor, statement, *a):
        seen.append(statement)
    event.listen(db.get_bind(), "before_cursor_execute", count)
    try:
        return fn(), len(seen)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", count)


def test_the_agents_listed_are_the_top_few_and_the_rest_is_one_entry(db, tenant, monkeypatch):
    monkeypatch.setattr(usage_report, "MAX_AGENTS_LISTED", 3)
    for i in range(10):
        _used(db, tenant, f"agent-{i}", tout=100 + i)
        _used(db, tenant, f"agent-{i}", tout=1, model="m2")
    _used(db, tenant, None, tout=5)
    db.commit()
    report, statements = _counted(db, lambda: usage_report.report(db, tenant.id))
    # The calls with no agent_id are always listed (null), never folded into the rest.
    assert [a["agent_id"] for a in report["by_agent"]] == ["agent-9", "agent-8", "agent-7", None]
    rest = report["other_agents"]
    assert rest["agents"] == 7      # seven named agents, all of them in the entry
    assert rest["calls"] == 7 * 2 and rest["completion_tokens"] == sum(101 + i for i in range(7))
    assert report["calls_without_an_agent_id"] == 1
    assert report["totals"]["calls"] == 21
    # However many agents: the count of statements does not move with them.
    for i in range(10, 40):
        _used(db, tenant, f"agent-{i}", tout=1)
    db.commit()
    again, statements_more = _counted(db, lambda: usage_report.report(db, tenant.id))
    assert statements_more == statements
    assert again["other_agents"]["agents"] == 7 + 30


def test_few_agents_have_no_rest(db, tenant):
    _used(db, tenant, "a", tout=1)
    db.commit()
    report = usage_report.report(db, tenant.id)
    assert report["other_agents"] is None and len(report["by_agent"]) == 1


def test_the_daily_buckets_are_utc_days_whatever_the_session_time_zone(db, tenant):
    """Three reads at 13:33 UTC, counted by the summary, were 0 in the chart on a
    database whose TimeZone is ahead of UTC."""
    now = datetime.now(timezone.utc).replace(hour=13, minute=33, second=0, microsecond=0)
    for _ in range(3):
        db.add(AuditEvent(tenant_id=tenant.id, ts=now, actor="rest", action=audit.READ, result="ok"))
    db.commit()
    try:
        for zone in ("UTC", "Pacific/Kiritimati", "Pacific/Pago_Pago"):
            db.execute(text(f"SET TIME ZONE '{zone}'"))
            daily = usage_report.requests_daily(db, tenant.id, days=1)
            assert daily["total_reads"] == 3, zone
            assert usage_report.requests_summary(db, tenant.id, days=1)["reads"] == 3
    finally:
        db.rollback()
        db.execute(text("SET TIME ZONE DEFAULT"))
