"""Agent B: the complaints desk.

A separate program, with nothing in common with Agent A except the store and
the key. A customer is angry and quoting the bot back, with a screenshot from
April. Was the bot wrong, or right at the time?

    python agent_auditor.py [--as-of 2026-04-01]
"""
import argparse

from _demo import CUSTOMER, SCREENSHOT_DAY, client, line


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=SCREENSHOT_DAY, help="the day to put the clock back to")
    day = ap.parse_args().as_of
    korely = client()

    print(f"Agent B (complaints desk), about {CUSTOMER}.\n")

    print("What we believe today:")
    for f in korely.get_facts(user_id=CUSTOMER):
        print(f"  {line(f)}")

    print(f"\nWhat we believed on {day}, the day of the screenshot:")
    for f in korely.get_facts(user_id=CUSTOMER, as_of=day):
        print(f"  {line(f)}")

    print("\nEverything, closed facts included (nothing is overwritten):")
    for f in korely.get_facts(user_id=CUSTOMER, include_invalidated=True):
        print(f"  {'closed ' if f.invalid_at else 'true   '} {line(f)}")

    ctx = korely.get_context(query="which plan is this customer on", user_id=CUSTOMER)
    print(f"\nWhat a model answering this customer today would be handed ({ctx.tokens} tokens):")
    # The block opens with a paragraph of instructions to the model; on a
    # shared screen that is a wall of text, so it is summarised in one line.
    head, sep, rest = ctx.context.partition("## Known facts")
    if sep and head.strip():
        print("  (a short instruction to the model, then:)")
    print("\n  " + (sep + rest if sep else ctx.context).replace("\n", "\n  "))


if __name__ == "__main__":
    main()
