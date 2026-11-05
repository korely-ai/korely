"""The angry customer.

A support bot told customer 4821 they had priority support. That was March. In
June the customer moved to a cheaper plan. Today they are angry and quoting the
bot back at you, with a screenshot.

In a store that keeps the current state, you look it up, it says Basic, and you
have no way to tell whether the customer is lying or your bot hallucinated. So
you refund, out of fear, and you learn nothing.

This script plays that out against a running install and prints what the store
knew on each date. It writes `demo.html` next to itself with the real captured
responses, which is the thing to record: a date slider, one question, and an
answer that changes without anybody editing anything.

    python examples/time_machine/run.py --url https://your-domain --key kor_self_...

Nothing here is special to the demo. Two writes and three reads, all documented
API calls, and you can run the curl by hand.
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# The story. Two facts about the same relation, months apart, phrased the way a
# support system would phrase them rather than the way a schema would.
SCRIPT = [
    ("2026-03-15", "Customer 4821 is on the Priority Support plan."),
    ("2026-06-20", "Customer 4821 moved to the Basic plan."),
]
USER = "demo-c4821"
ASK = ["2026-04-01", "2026-06-25", None]      # None = today


def call(url, key, path, *, body=None, insecure=False):
    req = urllib.request.Request(
        f"{url.rstrip('/')}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise SystemExit(f"{path} -> HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"cannot reach {url}: {e.reason}")


def _erase(url, key, insecure):
    """DELETE /v1/users/{id}/memories. Real erasure, so the demo cleans up after
    itself and the last two lines of output are the erasure being verified."""
    req = urllib.request.Request(
        f"{url.rstrip('/')}/v1/users/{USER}/memories",
        headers={"Authorization": f"Bearer {key}"}, method="DELETE")
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {}


def facts_at(url, key, as_of, *, insecure=False, include_invalidated=False):
    q = f"/v1/facts?user_id={USER}"
    if as_of:
        q += f"&as_of={as_of}"
    if include_invalidated:
        q += "&include_invalidated=true"
    return call(url, key, q, insecure=insecure).get("facts", [])


def line(f):
    closed = f.get("invalid_at")
    tail = f"  closed {closed[:10]}" if closed else ""
    return (f"    {f['subject']} -{f['predicate']}-> {f['object']}"
            f"   from {f['valid_from'][:10]}{tail}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="https://localhost")
    ap.add_argument("--key", required=True)
    ap.add_argument("--insecure", action="store_true",
                    help="accept a self-signed certificate (a local install)")
    ap.add_argument("--keep", action="store_true",
                    help="do not erase the demo user afterwards")
    a = ap.parse_args()
    ins = a.insecure

    print("Clearing anything left from a previous run.")
    _erase(a.url, a.key, ins)

    print("\nTwo writes, months apart.\n")
    for ts, content in SCRIPT:
        r = call(a.url, a.key, "/v1/memories",
                 body={"content": content, "user_id": USER, "timestamp": ts},
                 insecure=ins)
        print(f"  {ts}  {content}")
        print(f"            -> {r['id']}  status={r.get('status')}")

    print("\nWaiting for the worker to mine the facts", end="", flush=True)
    for _ in range(45):
        time.sleep(2)
        print(".", end="", flush=True)
        if len(facts_at(a.url, a.key, None, insecure=ins,
                        include_invalidated=True)) >= 2:
            break
    print()

    everything = facts_at(a.url, a.key, None, insecure=ins, include_invalidated=True)
    print("\nWhat the store holds, all of it:\n")
    for f in sorted(everything, key=lambda f: f["valid_from"]):
        print(line(f))

    captured = {}
    print("\nThe same question, asked at three different dates."
          "\nNothing is edited between them.\n")
    for as_of in ASK:
        rows = facts_at(a.url, a.key, as_of, insecure=ins)
        label = as_of or "today"
        captured[label] = rows
        print(f"  as_of={label}")
        if not rows:
            print("    (nothing)")
        for f in rows:
            print(line(f))
        print()

    out = Path(__file__).with_name("demo.html")
    out.write_text(_html(captured, everything))
    print(f"Wrote {out}. Open it and drag the slider; that is the recording.")

    if not a.keep:
        body = _erase(a.url, a.key, ins)
        print(f"\nErased the demo user: {body}")
        left = facts_at(a.url, a.key, None, insecure=ins, include_invalidated=True)
        print(f"Read back the most permissive way available: {len(left)} rows.")
    return 0


def _html(captured, everything) -> str:
    """A page built from the responses this run actually received.

    Built from captured data on purpose rather than calling the API from the
    browser: a demo page that carries an API key is a demo page that teaches
    people to ship an API key.
    """
    return """<!doctype html><meta charset=utf-8><title>What did it know?</title>
<style>
 body{font:16px/1.6 ui-sans-serif,system-ui;margin:0;background:#0f1115;color:#e7e9ee}
 .wrap{max-width:760px;margin:0 auto;padding:48px 24px}
 h1{font-size:28px;margin:0 0 8px} p.sub{color:#9aa3b2;margin:0 0 32px}
 .q{background:#171a21;border:1px solid #262b36;border-radius:10px;padding:20px 24px}
 .a{font-size:22px;margin:16px 0 0;min-height:34px}
 .none{color:#7c8496;font-style:italic}
 input[type=range]{width:100%;margin:28px 0 6px}
 .dates{display:flex;justify-content:space-between;color:#7c8496;font-size:13px}
 .now{color:#8ab4ff;font-weight:600}
 pre{background:#0b0d11;border:1px solid #262b36;border-radius:10px;
     padding:16px;overflow-x:auto;font-size:13px;color:#b9c0cd;margin-top:28px}
 .foot{color:#7c8496;font-size:14px;margin-top:24px}
</style>
<div class=wrap>
<h1>&ldquo;Your bot promised me priority support.&rdquo;</h1>
<p class=sub>Same database. Same question. Move the date.</p>
<div class=q>
  <div>What plan is customer 4821 on?</div>
  <div class=a id=answer></div>
  <input type=range id=slider min=0 max=2 value=0>
  <div class=dates><span id=label></span><span class=now>nothing was edited</span></div>
</div>
<pre id=raw></pre>
<p class=foot>Every line below came back from a real install. Reproduce it with
<code>examples/time_machine/run.py</code>.</p>
</div>
<script>
const DATA = __DATA__;
const KEYS = Object.keys(DATA);
const s = document.getElementById('slider');
s.max = KEYS.length - 1;
function render(){
  const k = KEYS[s.value], rows = DATA[k];
  document.getElementById('label').textContent = 'as of ' + k;
  document.getElementById('answer').innerHTML = rows.length
    ? rows.map(f => f.object).join(', ')
    : '<span class=none>nothing known yet</span>';
  document.getElementById('raw').textContent = JSON.stringify(rows, null, 2);
}
s.addEventListener('input', render); render();
</script>
""".replace("__DATA__", json.dumps(
        {k: [{"subject": f["subject"], "predicate": f["predicate"],
              "object": f["object"], "valid_from": f["valid_from"][:10],
              "invalid_at": (f.get("invalid_at") or None)}
             for f in v] for k, v in captured.items()}, indent=1))


if __name__ == "__main__":
    sys.exit(main())
