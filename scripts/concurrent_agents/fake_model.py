"""An OpenAI-compatible stand-in for the model, for scripts/concurrent_agents.sh.

Unlike scripts/two_processes/fake_model.py (which answers "no facts"), this one
exercises the whole fact-writing path with deterministic answers:

  - extraction (single and batch prompts of fact_extractor.py): one fact per
    sentence of the memory, read from the memory's own words:
        "Note cm-000123: <user> likes thing-4."      -> <user> likes thing-4
        "Note cm-000124: <user> lives in city-2."    -> <user> lives_in city-2
    with the sentence as `evidence`, so the grounding gate keeps it;
  - the contradiction judge (single and batch): a new "lives in X" closes the
    earlier "lives in Y" candidates; nothing else contradicts;
  - the vocabulary and the object-identity questions (objects.py,
    vocabulary.py): a new relation is its own relation, "likes" can hold many
    values, "lives in" one; two names are different things.

It counts, per memory tag (cm-NNNNNN), how many extraction calls carried it:
a memory that reaches the writer twice shows as 2. A prompt it does not
recognise is answered `{}` and listed at /stats, so a new model call in the
engine is seen, not hidden. No key, no network, nothing spent."""
import json, re, threading
from collections import Counter
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

TAG = re.compile(r"cm-\d{6}")
SENTENCE = re.compile(r"Note (cm-\d{6}): (.+?) (likes|lives in) ((?:thing|city)-\d+)\.")
END_USER = re.compile(r'identified as: "([^"]+)"')
BLOCK = re.compile(r"\[mem_id=([^\s\]]+)[^\]]*\]\n(.*?)(?=\n\[mem_id=|\Z)", re.S)

seen, calls, unknown, lock = Counter(), Counter(), Counter(), threading.Lock()


def facts_of(text):
    out = []
    for m in SENTENCE.finditer(text):
        _, user, verb, obj = m.groups()
        out.append({"subject": user, "subject_type": "person",
                    "predicate": "likes" if verb == "likes" else "lives_in",
                    "object": obj, "object_is_literal": False, "tense": "current",
                    "valid_from": "", "evidence": f"{user} {verb} {obj}", "confidence": 0.9})
    return out


# The prompt shows "Earlier memories of the same end user" before the memory
# ("Memory (date: ...):" or "Memories:"): context to read, not to count or extract.
MEMORY_HEADER = re.compile(r"\nMemor(?:y|ies)(?: \(date: [^)]*\))?:\n")


def the_memory(text):
    """The text after the last memory header: the memory (or memories) itself."""
    parts = MEMORY_HEADER.split(text)
    return parts[-1] if len(parts) > 1 else text


def extraction(text):
    if '"memories": [' in text:
        blocks = BLOCK.findall(the_memory(text))
        return {"memories": [{"mem_id": mid, "facts": facts_of(body)} for mid, body in blocks]}
    return {"facts": facts_of(the_memory(text))}


def judge_rows(text):
    return re.findall(r"- id=(\S+) \| (.*)", text)


def contradicts(new, row):
    """A new 'lives in X' closes an earlier 'lives in Y'. Only this one rule."""
    a, b = re.search(r"lives[ _]in (\S+)", new), re.search(r"lives[ _]in (\S+)", row)
    return bool(a and b and a.group(1) != b.group(1))


def judge(text):
    if '"results"' in text.split("NEW facts and their candidate", 1)[0]:
        out = []
        for m in re.finditer(r"\[new_id=(\S+)\] NEW: (.*?)\n  candidates:\n((?:  - id=[^\n]*\n?)*)", text, re.S):
            nid, new, rows = m.groups()
            out.append({"new_id": nid, "contradictions": [
                {"existing_id": i, "reason": "a person lives in one place"}
                for i, r in judge_rows(rows) if contradicts(new, r)]})
        return {"results": out}
    existing, new = text.split("NEW fact:", 1)[0], text.split("NEW fact:", 1)[1].strip().splitlines()[0]
    return {"contradictions": [{"existing_id": i, "reason": "a person lives in one place"}
                               for i, r in judge_rows(existing) if contradicts(new, r)]}


def answer(text):
    if "knowledge-graph facts" in text:
        users = TAG.findall(the_memory(text))
        with lock:
            calls["extraction"] += 1
            for t in users:
                seen[t] += 1
        return extraction(text)
    if "NEW fact" in text and "EXISTING facts" in text or "NEW facts and their candidate" in text:
        with lock:
            calls["judge"] += 1
        return judge(text)
    if '"many_values"' in text:
        replaces = "lives in" in text or "lives_in" in text
        return {"many_values": not replaces}
    if '{"same_as"' in text:
        return {"same_as": None}
    if '{"family"' in text:
        return {"family": "other"}
    if '{"past"' in text:
        return {"past": False}
    if '{"descriptions"' in text:
        return {"descriptions": [{"mention": int(n), "description": "a thing"}
                                 for n in re.findall(r"^Mention (\d+):", text, re.M)]}
    if '{"answers"' in text:
        return {"answers": [{"mention": int(n), "verdict": "different", "same_as": None}
                            for n in re.findall(r"^New mention (\d+):", text, re.M)]}
    if '{"pairs"' in text:
        return {"pairs": [{"pair": int(n), "verdict": "different"}
                          for n in re.findall(r"^Pair (\d+):", text, re.M)]}
    with lock:
        unknown[text.strip()[:80]] += 1
    return {}


async def chat(request):
    body = await request.json()
    text = "\n".join(m.get("content") or "" for m in body.get("messages", [])
                     if isinstance(m.get("content"), str))
    with lock:
        calls["total"] += 1
    return JSONResponse({
        "id": "x", "object": "chat.completion", "model": body.get("model"),
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": json.dumps(answer(text))}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105}})


async def models(request):
    return JSONResponse({"object": "list", "data": [{"id": "stand-in", "object": "model"}]})


async def stats(request):
    with lock:
        return JSONResponse({"calls": dict(calls), "memories_seen": len(seen),
                             "most_times_one_memory": max(seen.values()) if seen else 0,
                             "twice": sorted(t for t, n in seen.items() if n > 1)[:50],
                             "seen": sorted(seen), "unknown_prompts": dict(unknown)})

app = Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"]),
                        Route("/v1/models", models), Route("/stats", stats)])
