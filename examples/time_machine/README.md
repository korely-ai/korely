# The angry customer

A support bot told customer 4821 they had priority support. That was March. In
June they moved to a cheaper plan. Today they are angry and quoting the bot back
at you, with a screenshot.

In a store that keeps the current state, you look it up, it says Basic, and you
cannot tell whether the customer is lying or your bot hallucinated. So you
refund, out of fear, and you learn nothing. That is the demo.

```bash
python examples/time_machine/run.py --url https://your-domain --key kor_self_...
python examples/time_machine/run.py --url https://localhost --key kor_self_... --insecure
```

Output from a real run:

```
What the store holds, all of it:

    c4821 -is_on-> Priority Support plan   from 2026-03-15  closed 2026-06-20
    c4821 -is_on-> Basic plan              from 2026-06-20

The same question, asked at three different dates.
Nothing is edited between them.

  as_of=2026-04-01
    c4821 -is_on-> Priority Support plan
  as_of=2026-06-25
    c4821 -is_on-> Basic plan
  as_of=today
    c4821 -is_on-> Basic plan
```

The run ends by erasing the demo user and reading it back the most permissive
way the API allows, which prints `0 rows`. Two claims, one script, both
falsifiable by rerunning it.

## What it writes

`demo.html`, next to the script, built from the responses that run actually
received. One question, a date slider, and an answer that changes without
anybody editing anything. That is the thing to record: in a store that
overwrites, dragging the slider does nothing, because March is gone.

The page carries no API key and makes no requests. A demo page that ships a key
is a demo page that teaches people to ship a key.

## Why not a benchmark

Because the claim is not that this makes an agent smarter. It does not. The
claim is that when somebody asks what the system believed on a date, there is a
row rather than a reconstruction, and the honest way to show that is to let them
ask.
