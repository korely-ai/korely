"""Agent A: the support bot.

After each conversation it writes down, in plain English, what it learned,
with the day the conversation happened. That is all it does. It never writes a
fact, never says which sentence replaces which, and never reads anything back:
working out that June's plan closes March's is the store's job.

    python agent_support.py
"""
from _demo import CONVERSATIONS, CUSTOMER, client, wait_until_mined


def main() -> None:
    korely = client()
    print(f"Agent A (support bot) is logging its conversations with {CUSTOMER}.\n")
    for day, sentence in CONVERSATIONS:
        written = korely.add(sentence, user_id=CUSTOMER, agent_id="support-bot", timestamp=day)
        print(f"  {day}  \"{sentence}\"")
        print(f"            stored {written.id}, {written.status}", end="", flush=True)
        # One at a time, so each conversation is mined before the next one
        # arrives, the way they would arrive months apart.
        status, took = wait_until_mined(korely, written.id)
        print(f" -> {status} in {took:.0f}s")
    print("\nDone. Agent A has gone home; it never asked what the store made of it.")


if __name__ == "__main__":
    main()
