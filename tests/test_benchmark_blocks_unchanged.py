"""The blocks of a benchmark's shape are the ones of 1.0.21, byte for byte (1.0.22).

LoCoMo and LongMemEval write every turn into an end user's memory (each
speaker an end user, `scripts/bench_locomo.py`, `scripts/bench_longmemeval.py`)
and read them back with `GET /v1/context`, naming the end user and no agent
(`bench_locomo.Korely.context`), at 2,000 and 4,000 tokens. 1.0.22 changes
which memories make a run of consecutive turns (`context_experiments.
in_dialogue_order`): only an end user's, never a memory with no end user. A
benchmark's turns all have one, so its blocks are the ones of before.

The stores of those runs are not kept, so the benchmark's shape is built
here: two speakers' turns over three sessions, some with a relative time
dated as the extraction dates it (memory_core 2.9.0), facts of each speaker,
and a third end user whose turns carry no relative time, as most
LongMemEval sessions. Each is written with no agent and with one
(bench_locomo writes `agent_id` when asked) and read twice: with the agent
it was written with, at 2,000 and 4,000 tokens, and with the end user alone,
as bench_locomo reads, at 400, 800, 2,000 and 4,000 tokens. FINGERPRINTS are
the SHA-256 of each block's `context` as 1.0.21 (main 26e69e9e) built it
from this same file.
"""
from __future__ import annotations

import hashlib
import math
import random
from datetime import datetime, timedelta, timezone

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import context as ctx
from korely_agent.services import context_experiments as experiments
from korely_agent.services import vectors
from tests.conftest import seeded_project_id

SESSIONS = [datetime(2023, 5, 8, 13, 56, tzinfo=timezone.utc),
            datetime(2023, 5, 25, 13, 14, tzinfo=timezone.utc),
            datetime(2023, 6, 9, 19, 30, tzinfo=timezone.utc)]

# (speaker, session, text, its relative time as the extraction's model writes it)
TURNS = [
    ("caroline", 0, "assistant: Melanie: Hey Caroline! Good to see you! What's up with you?\n"
                    "user: Caroline: I went to a LGBTQ support group yesterday and it was so powerful.",
     {"text": "yesterday", "direction": "last", "unit": "day"}),
    ("melanie", 0, "assistant: Caroline: I went to a support group.\n"
                   "user: Melanie: That's great! I'm swamped with the kids and work.", None),
    ("caroline", 0, "assistant: Melanie: What did you take away from it?\n"
                    "user: Caroline: The transgender stories were so inspiring. I'm looking into "
                    "counseling as a career.", None),
    ("melanie", 0, "assistant: Caroline: Counseling, maybe.\n"
                   "user: Melanie: I painted a lake sunrise last year, it is special to me.", None),
    ("caroline", 0, "assistant: Melanie: You'd be a great counselor.\n"
                    "user: Caroline: Thanks! I'm keen on working with trans people.", None),
    ("melanie", 1, "assistant: Caroline: How was your week?\n"
                   "user: Melanie: I ran a charity race for mental health last Saturday.",
     {"text": "last Saturday", "direction": "last", "weekday": "saturday"}),
    ("caroline", 1, "assistant: Melanie: I ran a charity race.\n"
                    "user: Caroline: That's amazing! I'm planning an LGBTQ art show next month.",
     {"text": "next month", "direction": "next", "unit": "month"}),
    ("melanie", 1, "assistant: Caroline: An art show next month.\n"
                   "user: Melanie: I'll come! I'm also taking the kids camping soon.", None),
    ("caroline", 1, "assistant: Melanie: Camping sounds fun.\n"
                    "user: Caroline: I've been researching adoption agencies, it's my dream.", None),
    ("melanie", 1, "assistant: Caroline: Adoption agencies.\n"
                   "user: Melanie: My husband and I have been married for five years.", None),
    ("caroline", 2, "assistant: Melanie: How did the art show go?\n"
                    "user: Caroline: I gave a talk at a school event last week about my journey.",
     {"text": "last week", "direction": "last", "unit": "week"}),
    ("melanie", 2, "assistant: Caroline: I gave a talk at school.\n"
                   "user: Melanie: So proud of you! We went to the museum with the kids.", None),
    ("caroline", 2, "assistant: Melanie: The museum was great.\n"
                    "user: Caroline: My friends from the support group gave me a lot of strength.", None),
    ("melanie", 2, "assistant: Caroline: Friends give strength.\n"
                   "user: Melanie: Pottery is my favorite way to relax lately.", None),
]

# Enough turns more that a 2,000-token block is full, as LoCoMo's are (its
# saved blocks use 1,998 of 2,000 tokens): the packing reaches the budget,
# and cuts the last memory it shows.
ACTIVITIES = ["went hiking with the kids", "finished a painting of the beach",
              "read a book about self-acceptance", "cooked dinner for some friends",
              "visited my grandmother at the hospital", "signed up for a pottery class",
              "watched a documentary about community activists", "planted tomatoes in the garden"]
SAID = [None, None, {"text": "yesterday", "direction": "last", "unit": "day"}, None,
        {"text": "last week", "direction": "last", "unit": "week"}, None]
for i in range(48):
    speaker, other = ("caroline", "Melanie") if i % 2 else ("melanie", "Caroline")
    me = speaker.capitalize()
    time = SAID[i % len(SAID)]
    when = f" {time['text']}" if time else ""
    TURNS.append((speaker, i % 3,
                  f"assistant: {other}: What else is new with you, {me}? Tell me everything.\n"
                  f"user: {me}: I {ACTIVITIES[i % len(ACTIVITIES)]}{when}, and it reminded me "
                  f"of what matters to me (note {i}): family, art and the people around me.", time))

# A LongMemEval-like end user: long turns with no relative time.
PLAIN = [
    "user: I'm trying to organize my week. I have a workshop on time management and a "
    "webinar on data analysis with Python.\nassistant: Here is a plan for the week: block "
    "the mornings for the workshop and keep the afternoons for the webinar.",
    "user: Can you recommend resources for data visualization in Python?\nassistant: "
    "Matplotlib, Seaborn and Plotly are the three libraries most people start with.",
    "user: I adopted a cat named Luna and I'm looking for a good brand of food.\nassistant: "
    "Look for food with real meat first and no artificial colours.",
    "user: My commute to work takes 45 minutes each way by train.\nassistant: Podcasts and "
    "audiobooks make a long commute pass faster.",
]

FACTS = [("caroline", "Caroline", "attends", "LGBTQ support group"),
         ("caroline", "Caroline", "plans", "LGBTQ art show"),
         ("melanie", "Melanie", "paints", "lake sunrise"),
         ("melanie", "Melanie", "ran", "charity race"),
         ("lena", "user", "has_pet", "cat named Luna")]

QUESTIONS = [("support group", 11), ("art show", 12), ("charity race", 13), ("adoption", 14)]
PLAIN_QUESTIONS = [("workshop or webinar", 21), ("cat food", 22)]
BUDGETS = (2000, 4000)
READ_BUDGETS = (400, 800, 2000, 4000)
AGENTS = (None, "locomo-bench")


def unit(seed: int, near: list | None = None, cos: float = 0.0) -> list:
    """A deterministic unit vector over eight dimensions, `cos` from `near`
    when given: distinct cosines, so no ranking rests on a tie."""
    rnd = random.Random(seed)
    v = [rnd.uniform(-1, 1) for _ in range(8)]
    if near is not None:
        dot = sum(a * b for a, b in zip(v, near))
        v = [a - dot * b for a, b in zip(v, near)]
        n = math.sqrt(sum(a * a for a in v))
        v = [cos * b + math.sqrt(1 - cos * cos) * a / n for a, b in zip(v, near)]
    n = math.sqrt(sum(a * a for a in v))
    return [a / n for a in v] + [0.0] * (EMBEDDING_DIM - 8)


def dated(text: str, when: datetime, time) -> dict | None:
    from korely_agent.services.memory_core.relative_time import annotate

    if time is None:
        return None
    column, dropped = annotate([time], when.date(), text=text)
    assert not dropped and column["times"], (text, dropped)
    return column


def fill(db, tenant, agent):
    project = seeded_project_id(db, tenant.id)
    rows = []
    for i, (who, session, text, time) in enumerate(TURNS):
        when = SESSIONS[session] + timedelta(minutes=i)
        rows.append(AgentMemory(tenant_id=tenant.id, project_id=project, end_user_id=who,
                                agent_id=agent, content=text, event_time=when,
                                created_at=datetime(2026, 11, 1, tzinfo=timezone.utc) + timedelta(seconds=i),
                                relative_times=dated(text, when, time),
                                embedding=unit(100 + i), embedding_status=vectors.DONE))
    for i, text in enumerate(PLAIN):
        when = SESSIONS[i % 3] + timedelta(hours=1, minutes=i)
        rows.append(AgentMemory(tenant_id=tenant.id, project_id=project, end_user_id="lena",
                                agent_id=agent, content=text, event_time=when,
                                created_at=datetime(2026, 11, 2, tzinfo=timezone.utc) + timedelta(seconds=i),
                                embedding=unit(200 + i), embedding_status=vectors.DONE))
    for i, (who, subject, predicate, obj) in enumerate(FACTS):
        rows.append(AgentFact(tenant_id=tenant.id, project_id=project, end_user_id=who,
                              agent_id=agent, subject_label=subject, subject_type="person",
                              predicate_raw=predicate, predicate_canonical=predicate,
                              object_label=obj, object_is_literal=True, confidence=0.9,
                              valid_from=SESSIONS[0], embedding=unit(300 + i)))
    db.add_all(rows)
    db.commit()


def blocks(db, tenant, monkeypatch) -> dict:
    """{case: the block's `context`} for every case, the same on both trees."""
    monkeypatch.setattr(experiments, "DIALOGUE_ORDER", True)
    cases = [(who, q, seed) for who in ("caroline", "melanie") for q, seed in QUESTIONS]
    cases += [("lena", q, seed) for q, seed in PLAIN_QUESTIONS]
    out = {}
    for agent in AGENTS:
        db.query(AgentFact).delete()
        db.query(AgentMemory).delete()
        db.commit()
        fill(db, tenant, agent)
        # Read with the agent it was written with, and, written with one, as
        # bench_locomo reads: the end user alone.
        reads = [(agent, agent or "-", BUDGETS)]
        if agent:
            reads.append((None, "read-without-agent", READ_BUDGETS))
        for who, question, seed in cases:
            q = unit(seed)
            monkeypatch.setattr(ctx, "embed", lambda *a, _q=q, **k: _q)
            for read_agent, name, budgets in reads:
                for budget in budgets:
                    block = ctx.assemble_context(db, tenant.id, query=question, end_user_id=who,
                                                 agent_id=read_agent, token_budget=budget)
                    out[f"{name}/{who}/{question}/{budget}"] = block["context"]
    return out


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


FINGERPRINTS = {
    "-/caroline/adoption/2000":
        "054e8c00af541c8ab42ad779e4b5a64b1bc00376f46725ebed870e83e3a047af",
    "-/caroline/adoption/4000":
        "eb2ce4ae5740d4ac4e4a85b9fd247787dd7b74873279657d52910ebb6e46a677",
    "-/caroline/art show/2000":
        "6c5688ffca993fe393b214f823f7940d1ff1a3282a24754b1f579a384f46e07f",
    "-/caroline/art show/4000":
        "356093bada30daa20bd26566aab302205d19655e3068972b9e566a2063b6d4b7",
    "-/caroline/charity race/2000":
        "aadbc2fcbbec69b84af14678ea6b96759d5624b0cdf1b1786b9d6446de78dd54",
    "-/caroline/charity race/4000":
        "726863acb9b8db6b71bf39d1f80bcb955ae89e928fe69a2c10428218c67e88c9",
    "-/caroline/support group/2000":
        "64b3181e4d21f64533265ad63a5425b5ea423e4cea3cf073b32585b431b36748",
    "-/caroline/support group/4000":
        "6f13aa226925edbab0ac9d18f0fcbd30912107ffe4c98376e4dfecd5d10d3f56",
    "-/lena/cat food/2000":
        "bd230fe420cf061ec66d1e8606df6afb6e65e34549fed28828c8fc7174229689",
    "-/lena/cat food/4000":
        "bd230fe420cf061ec66d1e8606df6afb6e65e34549fed28828c8fc7174229689",
    "-/lena/workshop or webinar/2000":
        "bd230fe420cf061ec66d1e8606df6afb6e65e34549fed28828c8fc7174229689",
    "-/lena/workshop or webinar/4000":
        "bd230fe420cf061ec66d1e8606df6afb6e65e34549fed28828c8fc7174229689",
    "-/melanie/adoption/2000":
        "372c5470251f5672d0007cd630fa794270e218c6015bd3a491bb1d264f5f80fd",
    "-/melanie/adoption/4000":
        "ccf6aced7f5456260022540915a14228521540bbabb0500f49ed28d39438e7f7",
    "-/melanie/art show/2000":
        "6a0cbe708fdd51c5b9e431d7e37ab858f1b099e3daa5152dbb4943a6fbe6fbd2",
    "-/melanie/art show/4000":
        "69451d9773300676c0819ace8c9d5ab49dd2f3bf81eecca084ae8e5a1166835d",
    "-/melanie/charity race/2000":
        "35084e03ffdcc97b8acd18793cc340bf6b097e004c02f0031c3adf498725cabc",
    "-/melanie/charity race/4000":
        "6b4ff4e8fdd645ed3f2255819e2a13a91b86c2425d4ae14468048747e23dc3b4",
    "-/melanie/support group/2000":
        "7fecddbce36f3066dd040f5560bbc49c1460dcac61ecffb40fff92ca75ceca41",
    "-/melanie/support group/4000":
        "031ce5227579c446947e9c2c187c8df3c9ab5ec6e43f9258509375959adb6cae",
    "locomo-bench/caroline/adoption/2000":
        "054e8c00af541c8ab42ad779e4b5a64b1bc00376f46725ebed870e83e3a047af",
    "locomo-bench/caroline/adoption/4000":
        "eb2ce4ae5740d4ac4e4a85b9fd247787dd7b74873279657d52910ebb6e46a677",
    "locomo-bench/caroline/art show/2000":
        "6c5688ffca993fe393b214f823f7940d1ff1a3282a24754b1f579a384f46e07f",
    "locomo-bench/caroline/art show/4000":
        "356093bada30daa20bd26566aab302205d19655e3068972b9e566a2063b6d4b7",
    "locomo-bench/caroline/charity race/2000":
        "aadbc2fcbbec69b84af14678ea6b96759d5624b0cdf1b1786b9d6446de78dd54",
    "locomo-bench/caroline/charity race/4000":
        "726863acb9b8db6b71bf39d1f80bcb955ae89e928fe69a2c10428218c67e88c9",
    "locomo-bench/caroline/support group/2000":
        "64b3181e4d21f64533265ad63a5425b5ea423e4cea3cf073b32585b431b36748",
    "locomo-bench/caroline/support group/4000":
        "6f13aa226925edbab0ac9d18f0fcbd30912107ffe4c98376e4dfecd5d10d3f56",
    "locomo-bench/lena/cat food/2000":
        "bd230fe420cf061ec66d1e8606df6afb6e65e34549fed28828c8fc7174229689",
    "locomo-bench/lena/cat food/4000":
        "bd230fe420cf061ec66d1e8606df6afb6e65e34549fed28828c8fc7174229689",
    "locomo-bench/lena/workshop or webinar/2000":
        "bd230fe420cf061ec66d1e8606df6afb6e65e34549fed28828c8fc7174229689",
    "locomo-bench/lena/workshop or webinar/4000":
        "bd230fe420cf061ec66d1e8606df6afb6e65e34549fed28828c8fc7174229689",
    "locomo-bench/melanie/adoption/2000":
        "372c5470251f5672d0007cd630fa794270e218c6015bd3a491bb1d264f5f80fd",
    "locomo-bench/melanie/adoption/4000":
        "ccf6aced7f5456260022540915a14228521540bbabb0500f49ed28d39438e7f7",
    "locomo-bench/melanie/art show/2000":
        "6a0cbe708fdd51c5b9e431d7e37ab858f1b099e3daa5152dbb4943a6fbe6fbd2",
    "locomo-bench/melanie/art show/4000":
        "69451d9773300676c0819ace8c9d5ab49dd2f3bf81eecca084ae8e5a1166835d",
    "locomo-bench/melanie/charity race/2000":
        "35084e03ffdcc97b8acd18793cc340bf6b097e004c02f0031c3adf498725cabc",
    "locomo-bench/melanie/charity race/4000":
        "6b4ff4e8fdd645ed3f2255819e2a13a91b86c2425d4ae14468048747e23dc3b4",
    "locomo-bench/melanie/support group/2000":
        "7fecddbce36f3066dd040f5560bbc49c1460dcac61ecffb40fff92ca75ceca41",
    "locomo-bench/melanie/support group/4000":
        "031ce5227579c446947e9c2c187c8df3c9ab5ec6e43f9258509375959adb6cae",
    "read-without-agent/caroline/adoption/2000":
        "054e8c00af541c8ab42ad779e4b5a64b1bc00376f46725ebed870e83e3a047af",
    "read-without-agent/caroline/adoption/400":
        "cf1c76104b52df6876e3f28e9b6ace53da89400790a99cdbdd81b3565bdf0815",
    "read-without-agent/caroline/adoption/4000":
        "eb2ce4ae5740d4ac4e4a85b9fd247787dd7b74873279657d52910ebb6e46a677",
    "read-without-agent/caroline/adoption/800":
        "275b093ecbc4c2a3041bcc86a4e7889cb63da4707b0748278b69296e7053fa72",
    "read-without-agent/caroline/art show/2000":
        "6c5688ffca993fe393b214f823f7940d1ff1a3282a24754b1f579a384f46e07f",
    "read-without-agent/caroline/art show/400":
        "655643930d3eb072b04bc970747f7ab0f2561fce4879de47ea03c19b9c6258f7",
    "read-without-agent/caroline/art show/4000":
        "356093bada30daa20bd26566aab302205d19655e3068972b9e566a2063b6d4b7",
    "read-without-agent/caroline/art show/800":
        "5e7bd0f499fe469bfdff7961c38d82e1d307b68bf490437a7b7908ce77f7ecaf",
    "read-without-agent/caroline/charity race/2000":
        "aadbc2fcbbec69b84af14678ea6b96759d5624b0cdf1b1786b9d6446de78dd54",
    "read-without-agent/caroline/charity race/400":
        "b138925ca4efd268f67f46aaae72becde1fc0572e90eeef06dc03889d2beb297",
    "read-without-agent/caroline/charity race/4000":
        "726863acb9b8db6b71bf39d1f80bcb955ae89e928fe69a2c10428218c67e88c9",
    "read-without-agent/caroline/charity race/800":
        "0792bc1ab5b7ecb59602ed05c7b0129f136bf3fe631baeb04a20dbeaa0a8ed27",
    "read-without-agent/caroline/support group/2000":
        "64b3181e4d21f64533265ad63a5425b5ea423e4cea3cf073b32585b431b36748",
    "read-without-agent/caroline/support group/400":
        "6defcd59166f8e949ba9b9c531c342e68c4c6578d5cea120467cad573c7971fa",
    "read-without-agent/caroline/support group/4000":
        "6f13aa226925edbab0ac9d18f0fcbd30912107ffe4c98376e4dfecd5d10d3f56",
    "read-without-agent/caroline/support group/800":
        "5418754633c8cd6e48158c229d1c36f1b8164435ddb656007b1208242c8f8f7c",
    "read-without-agent/lena/cat food/2000":
        "bd230fe420cf061ec66d1e8606df6afb6e65e34549fed28828c8fc7174229689",
    "read-without-agent/lena/cat food/400":
        "ea7b14f0863b6289f4358112f36b9ed95b4ea30c5fa004f3555be925b2f2cc88",
    "read-without-agent/lena/cat food/4000":
        "bd230fe420cf061ec66d1e8606df6afb6e65e34549fed28828c8fc7174229689",
    "read-without-agent/lena/cat food/800":
        "bd230fe420cf061ec66d1e8606df6afb6e65e34549fed28828c8fc7174229689",
    "read-without-agent/lena/workshop or webinar/2000":
        "bd230fe420cf061ec66d1e8606df6afb6e65e34549fed28828c8fc7174229689",
    "read-without-agent/lena/workshop or webinar/400":
        "ea7b14f0863b6289f4358112f36b9ed95b4ea30c5fa004f3555be925b2f2cc88",
    "read-without-agent/lena/workshop or webinar/4000":
        "bd230fe420cf061ec66d1e8606df6afb6e65e34549fed28828c8fc7174229689",
    "read-without-agent/lena/workshop or webinar/800":
        "bd230fe420cf061ec66d1e8606df6afb6e65e34549fed28828c8fc7174229689",
    "read-without-agent/melanie/adoption/2000":
        "372c5470251f5672d0007cd630fa794270e218c6015bd3a491bb1d264f5f80fd",
    "read-without-agent/melanie/adoption/400":
        "afef6b99ea22df0e5da760da696b606ca5362c80055f6b397ddcd690c9d776b4",
    "read-without-agent/melanie/adoption/4000":
        "ccf6aced7f5456260022540915a14228521540bbabb0500f49ed28d39438e7f7",
    "read-without-agent/melanie/adoption/800":
        "9b4165762ec391f179cede07a694a7223980681ead79ffd6eae2c52a2b1d7fb1",
    "read-without-agent/melanie/art show/2000":
        "6a0cbe708fdd51c5b9e431d7e37ab858f1b099e3daa5152dbb4943a6fbe6fbd2",
    "read-without-agent/melanie/art show/400":
        "584b75bbccc92cbcdd812ab961626c4a32779077097e805d34967464db81e485",
    "read-without-agent/melanie/art show/4000":
        "69451d9773300676c0819ace8c9d5ab49dd2f3bf81eecca084ae8e5a1166835d",
    "read-without-agent/melanie/art show/800":
        "7c5c6877e45621215a05b5899560c7c64a3e492778df5f2789a09cf6c4e0b801",
    "read-without-agent/melanie/charity race/2000":
        "35084e03ffdcc97b8acd18793cc340bf6b097e004c02f0031c3adf498725cabc",
    "read-without-agent/melanie/charity race/400":
        "4c0507a8127640d99b68e18a27a699a562d66dc5df4b6f291d42e840947c797b",
    "read-without-agent/melanie/charity race/4000":
        "6b4ff4e8fdd645ed3f2255819e2a13a91b86c2425d4ae14468048747e23dc3b4",
    "read-without-agent/melanie/charity race/800":
        "d9908e4b4957fcf33e11e4123854173caf5dc86d7148cf219ea5570ada1a4bef",
    "read-without-agent/melanie/support group/2000":
        "7fecddbce36f3066dd040f5560bbc49c1460dcac61ecffb40fff92ca75ceca41",
    "read-without-agent/melanie/support group/400":
        "dffb37f24d0bdb0af07f43c2faf48d8d589cf9d6ef0722d063de6262b0c3bf5a",
    "read-without-agent/melanie/support group/4000":
        "031ce5227579c446947e9c2c187c8df3c9ab5ec6e43f9258509375959adb6cae",
    "read-without-agent/melanie/support group/800":
        "8be85c6e533969b6fdbf21a4cc477eeb52b9b5c5088f88d5cdeb3051ffdfb88e",
}


def test_the_benchmark_blocks_are_the_ones_of_before(db, tenant, monkeypatch):
    built = blocks(db, tenant, monkeypatch)
    assert len(built) == len(FINGERPRINTS) == 80
    changed = [case for case, text in built.items() if fingerprint(text) != FINGERPRINTS[case]]
    assert changed == [], changed
    assert all("## Relevant memories" in text for text in built.values())
