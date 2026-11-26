"""Does "the same thing?" unite what is one thing and keep apart what only looks alike?

The bench of memory_core 6.1.0 (`objects.py`, the switch
`services/facts/objects.SAME_OBJECT`): scripts/bench_same_object.json, one
end user's memory per persona (its facts, each with the memory's words) and
new mentions asked against it, the way the write asks them: a shortlist of
the memory's objects in the mention's relation family, never people, by
vectors (local EmbeddingGemma), then the engine's own `objects.decide`
(describe, select, then the chosen pair alone framed the other way round),
with the engine's prompts.

Two things are measured, and only the pair means anything: WRONG unions (a
mention linked to a thing it is not: the expensive direction) and MISSED
unions (one thing left as two: a duplicate), each with its Wilson 95%
interval. The hard negatives (kind hard_negative) carry no disambiguator in
their own sentence; `against` is the stored object each was built against,
and "reached" counts those whose shortlist held it (the ones the writer
actually had to keep apart). And the shortlist on its own (blocking recall).

    # vectors only, free (the local model mounted where the image expects it)
    docker run --rm -v <model dir>:/opt/korely-models/embeddinggemma-300m:ro \
      -v $PWD:/w -w /w -e PYTHONPATH=/w korely-agent-dev python scripts/measure_same_object.py
    # with a writer: --region global (GEMINI_KEY, gemini-3.1-flash-lite) or
    # --region eu (SCW_KEY, Scaleway gpt-oss-120b); --batch 4 asks four
    # mentions of one memory in one write; --vectors name for the bare name
    ... -e GEMINI_KEY=... python scripts/measure_same_object.py --region global --cap 0.5

Each run prints the cost from the tokens the provider counted, and stops
before it would pass --cap.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from korely_agent.services.memory_core import identity  # noqa: E402
from korely_agent.services.memory_core import objects as O  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# Price per million tokens (input, output), in euro: Gemini 3.1 Flash-Lite at
# $0.25 / $1.50 (config.py's example) taken 1:1, Scaleway gpt-oss-120b
# EUR 0.15 / 0.60 (Scaleway's price list).
PRICES = {"global": (0.25, 1.50), "eu": (0.15, 0.60)}
MODELS = {"global": "gemini-3.1-flash-lite", "eu": "gpt-oss-120b"}


def _load(path):
    with open(path, encoding="utf-8") as f:
        b = json.load(f)
    memories = {}
    for name, p in b["personas"].items():
        s = p["subject"]
        facts = [dict(subject=s, relation=r, object=o, family=fam, words=w) for r, o, fam, w in p["facts"]]
        if p.get("filler", True):
            facts += [dict(subject=s, relation=r, object=o, family=fam, words=w.replace("{S}", s))
                      for r, o, fam, w in b["filler"]]
        memories[name] = facts
    return memories, b["mentions"]


def _fact_text(subject, relation, obj):
    # services/facts/service.fact_text: "subject predicate object"
    return f"{subject} {relation} {obj}".strip()


class _Vectors:
    def __init__(self, cache):
        self.cache_path = cache
        self.cache = {}
        if cache and os.path.exists(cache):
            with open(cache) as f:
                self.cache = json.load(f)

    def __call__(self, text, kind):
        from korely_agent.core import local_embedder as L

        key = f"{kind}|{text}"
        if key not in self.cache:
            prompt = L.DOCUMENT if kind == "doc" else L.SIMILARITY
            self.cache[key] = L.embed(text, prompt)[0]
        return self.cache[key]

    def save(self):
        if self.cache_path:
            with open(self.cache_path, "w") as f:
                json.dump(self.cache, f)


def _cos(a, b):
    return 1.0 - sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def wilson(k, n, z=1.96):
    """The Wilson score interval of k in n, 95% (Wilson 1927)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _rate(k, n):
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100 * k / max(1, n):.1f}% [95% {100 * lo:.1f}%, {100 * hi:.1f}%]"


def shortlist(m, facts, vec, how, subject, k=O.SHORTLIST, keep_same_name=False):
    """The memory's objects nearest to the mention, distinct, nearest first,
    as `objects.nearest_objects` reads them: the facts of the mention's
    family only, never a family `objects.NEVER_ASKED`, by the fact's text
    (`context`) or by the bare name (`name`)."""
    fam = m.get("family") or "other"
    facts = [f for f in facts if (f.get("family") or "other") == fam]
    if how == "context":
        q = vec(_fact_text(subject, m["relation"], m["object"]), "doc")
        scored = [(_cos(q, vec(_fact_text(f["subject"], f["relation"], f["object"]), "doc")), f) for f in facts]
    else:
        q = vec(m["object"], "sim")
        scored = [(_cos(q, vec(f["object"], "sim")), f) for f in facts]
    scored.sort(key=lambda x: x[0])
    out, seen = [], set()
    for d, f in scored:
        key = f["object"].strip().lower()
        if key in seen or (key == m["object"].strip().lower() and not keep_same_name):
            continue
        seen.add(key)
        out.append((d, f))
        if len(out) >= k:
            break
    return out


def _mention(m, subject):
    return O.Mention(name=m["object"], subject=subject, relation=m["relation"], family=m.get("family"),
                     words=m.get("words"))


def _candidate(f):
    return O.Candidate(name=f["object"], mention=O.Mention(
        name=f["object"], subject=f["subject"], relation=f["relation"], family=f["family"],
        words=O.words_around(f["words"], f["object"])))


def _provider(region):
    from korely_agent.core import llm

    if region == "global":
        return llm.GeminiProvider(os.environ["GEMINI_KEY"], force_model=MODELS["global"], region="global")
    return llm.MistralProvider(os.environ["SCW_KEY"], "https://api.scaleway.ai/v1", force_model=MODELS["eu"],
                               vendor="scaleway", region="eu")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bench", default=os.path.join(HERE, "bench_same_object.json"))
    p.add_argument("--region", choices=("global", "eu"), default=None)
    p.add_argument("--vectors", choices=("context", "name"), default="context")
    p.add_argument("--batch", type=int, default=1)
    p.add_argument("--think", action="store_true")
    p.add_argument("--only", default=None, help="only the mentions of these kinds, comma separated")
    p.add_argument("--cache", default=None, help="a JSON file to keep the vectors in between runs")
    p.add_argument("--out", default=None, help="a JSON file for every answer")
    p.add_argument("--cap", type=float, default=0.30, help="stop before the run's cost passes this, euro")
    a = p.parse_args()

    memories, mentions = _load(a.bench)
    if a.only:
        mentions = [m for m in mentions if m["kind"] in a.only.split(",")]
    subjects = {}
    with open(a.bench, encoding="utf-8") as f:
        for name, persona in json.load(f)["personas"].items():
            subjects[name] = persona["subject"]
    vec = _Vectors(a.cache)

    def asked(m):
        return identity.valid_name(m["object"]) and (m.get("family") or "other") not in O.NEVER_ASKED

    # 1. the shortlist alone (blocking recall, and the negatives' reach), free
    for how in ("context", "name"):
        found, ranks = 0, Counter()
        same = [m for m in mentions if m["kind"] == "same" and asked(m)]
        for m in same:
            sl = shortlist(m, memories[m["persona"]], vec, how, subjects[m["persona"]])
            names = [f["object"] for _, f in sl]
            if m["expected"] in names:
                found += 1
                ranks[names.index(m["expected"]) + 1] += 1
        hard = [m for m in mentions if m["kind"] == "hard_negative" and asked(m)]
        reach = sum(1 for m in hard if m["against"] in [f["object"] for _, f in shortlist(
            m, memories[m["persona"]], vec, how, subjects[m["persona"]])])
        print(f"shortlist by {how:7s}: {found}/{len(same)} same-thing pairs in the top {O.SHORTLIST} "
              f"(rank 1..{O.SHORTLIST}: {[ranks[i] for i in range(1, O.SHORTLIST + 1)]}); "
              f"hard negatives reaching their false friend: {reach}/{len(hard)}")
    vec.save()
    if a.region is None:
        return 0

    # 2. the questions
    provider = _provider(a.region)
    spent = {"in": 0, "out": 0, "calls": 0, "seconds": 0.0, "largest": 0.0}
    pin, pout = PRICES[a.region]

    def cost():
        return (spent["in"] * pin + spent["out"] * pout) / 1e6

    def ask(prompt, think):
        if cost() + spent["largest"] > a.cap:
            raise SystemExit(f"stopped: the next call would pass {a.cap} euro (spent {cost():.4f})")
        before = cost()
        t = time.time()
        r = provider.complete(messages=[{"role": "user", "content": prompt}], model=MODELS[a.region],
                              max_tokens=4096, temperature=0.0, json_only=True, reasoning=think)
        spent["seconds"] += time.time() - t
        spent["in"] += r.prompt_tokens
        spent["out"] += r.completion_tokens
        spent["calls"] += 1
        spent["largest"] = max(spent["largest"], cost() - before)
        return r.text

    by_persona = defaultdict(list)
    for m in mentions:
        by_persona[m["persona"]].append(m)
    results = []
    for persona, ms in by_persona.items():
        items = []
        for m in ms:
            if not asked(m):
                # The write never asks it (`objects.new_mentions`: a name that
                # cannot be one, a comma for one; the people family): apart.
                results.append(dict(m, shortlist=[], answer="not asked"))
                continue
            probe = m["kind"] == "probe"
            sl = shortlist(m, memories[persona], vec, a.vectors, subjects[persona], keep_same_name=probe)
            if probe and not any(f["object"].lower() == m["object"].lower() for _, f in sl):
                same_name = [f for f in memories[persona] if f["object"].lower() == m["object"].lower()]
                sl = ([(0.0, same_name[0])] + sl[:O.SHORTLIST - 1]) if same_name else sl
            if not sl:
                results.append(dict(m, shortlist=[], answer="no candidate"))
                continue
            items.append((m, sl))
        for i in range(0, len(items), max(1, a.batch)):
            chunk = items[i:i + max(1, a.batch)]
            q = [(_mention(m, subjects[persona]), [_candidate(f) for _, f in sl]) for m, sl in chunk]
            decisions = O.decide(ask, q, think=a.think, severe=True)
            for (m, sl), d in zip(chunk, decisions):
                names = [f["object"] for _, f in sl]
                got = names[d.answer] if isinstance(d.answer, int) else d.answer
                results.append(dict(m, shortlist=names, answer=got, reason=O.reason_of(d)))

    apart = (O.DIFFERENT, O.UNSURE, "not asked", "no candidate")

    def wrong(r):
        return r["answer"] not in apart and r["answer"] != r["expected"]

    print(f"\nregion {a.region} ({MODELS[a.region]}), vectors by {a.vectors}, batch {a.batch}, think {a.think}")
    hard = [r for r in results if r["kind"] == "hard_negative"]
    reached = [r for r in hard if r.get("against") in r["shortlist"]]
    print(f"hard negatives, WRONG unions: {_rate(sum(map(wrong, hard)), len(hard))}")
    print(f"  of those whose false friend was offered: {_rate(sum(map(wrong, reached)), len(reached))}")
    for cls in sorted({r.get("class") for r in hard}):
        rs = [r for r in hard if r.get("class") == cls]
        print(f"  {cls:10s} {_rate(sum(map(wrong, rs)), len(rs))}  "
              + ", ".join(f"{r['id']} {r['object']}->{r['answer']}" for r in rs if wrong(r)))
    hinted = [r for r in results if r["kind"] in ("false_friend", "different")]
    print(f"negatives with a hint in their sentence (29 Oct), WRONG: {_rate(sum(map(wrong, hinted)), len(hinted))}  "
          + ", ".join(f"{r['id']} {r['object']}->{r['answer']}" for r in hinted if wrong(r)))
    every = hard + hinted
    print(f"every negative, WRONG unions: {_rate(sum(map(wrong, every)), len(every))}")
    for cls, label in (("not_paraphrase", "positives in another fact (30 Oct)"),
                       (None, "positives of 29 Oct (often the stored fact translated)")):
        rs = [r for r in results if r["kind"] == "same" and r.get("class") == cls]
        missed = [r for r in rs if r["answer"] != r["expected"]]
        print(f"{label}: missed {_rate(len(missed), len(rs))}, wrong {sum(map(wrong, rs))}  "
              + ", ".join(f"{r['id']} {r['object']}->{r['answer']}" for r in missed))
    for kind in ("ambiguous", "probe"):
        print(f"  {kind}: " + ", ".join(f"{r['id']} {r['object']}->{r['answer']} (exp {r['expected']})"
                                        for r in results if r["kind"] == kind))
    print(f"calls {spent['calls']}, tokens in {spent['in']} out {spent['out']}, "
          f"cost EUR {cost():.4f}, per call EUR {cost() / max(1, spent['calls']):.5f}, "
          f"{spent['seconds'] / max(1, spent['calls']):.2f} s per call")
    if a.out:
        with open(a.out, "w") as f:
            json.dump(dict(results=results, spent=spent, cost=cost()), f, ensure_ascii=False, indent=1)
    vec.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
