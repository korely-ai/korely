"""The JSON object in a model's answer, read the same way everywhere (memory_core 2.11.0).

The rule on reading a model's JSON (CONTRIBUTING.md), kept here because this
folder imports nothing from the application. Until 11 Oct 2026 the folder
was copied byte for byte into korely-agent and the two copies answered the
same on the same texts (a shared test ran both on one list of cases).

Why a rule and not `json.loads`. A model asked for JSON
(`response_mime_type="application/json"`) still sometimes writes something
around the object. Seen on 2026-10-01 with gemini-3.1-flash-lite, which since
6 Oct runs all three stages of an Agents write: the object closed with one
brace too many (`{"facts": []}\\n}`). `json.loads` refuses the whole answer,
and the extractor's old fallback (cut from the first brace to the end) refused
it again, so a memory that held no facts ended 'error'. The rule: the whole
text when it parses; otherwise every JSON object written in it, and they must
all say the same thing. One object with stray text around it is that object;
the same object written twice is that object; two different objects are no
answer, because nothing says which one the model meant (the same reading as
`vocabulary._json`, BUG-198, 2026-09-29).
"""
from __future__ import annotations

import json
from typing import Any, List, Optional


def json_objects(text: str) -> List[dict]:
    """Every JSON object written in `text`, in order: `json.JSONDecoder.raw_decode`
    from each "{" that starts one, going on after its end."""
    dec = json.JSONDecoder()
    found: List[dict] = []
    i = text.find("{")
    while i != -1:
        try:
            o, end = dec.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(o, dict):
            found.append(o)
        i = text.find("{", end)
    return found


def read_object(text: Optional[str]) -> Optional[Any]:
    """The answer's JSON: the whole text when it parses (a ```json fence
    removed), otherwise the one object it contains, written once or repeated
    identically. None when there is none, or when two objects disagree."""
    if not text:
        return None
    text = text.strip()
    for prefix in ("```json", "```"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    objects = json_objects(text)
    if objects and all(o == objects[0] for o in objects):
        return objects[0]
    return None


def read_answer(raw: Optional[str], reader=None) -> Optional[Any]:
    """The JSON value of a model's answer for the readers of this folder (the
    extractor and the judge): `read_object` first, then the engine's own
    `reader` when it passes one and the rule found nothing. A `reader` may
    return the value itself or, as every engine did before 2.11.0, the JSON
    text to parse. None when nothing readable came out: never an empty
    answer."""
    value = read_object(raw)
    if value is not None or reader is None or not raw:
        return value
    try:
        value = reader(raw)
        if isinstance(value, (str, bytes, bytearray)):
            value = json.loads(value)
    except Exception:  # noqa: BLE001 - an engine's reader that fails reads nothing
        return None
    return value
