"""Start the story again: erase the demo customer.

    python reset_demo.py

It is the real erasure, `DELETE /v1/users/c4821/memories`, the one a data
subject's request uses: memories and facts go, history included, and the reply
counts them. Worth showing once on purpose.
"""
from _demo import CUSTOMER, client


def main() -> None:
    korely = client()
    r = korely.delete_all(user_id=CUSTOMER)
    print(f"erased {CUSTOMER}: {r.memories_deleted} memories and {r.facts_deleted} facts, "
          f"erasure {r.erasure} (audit row {r.audit_id}, which holds counts and no content)")
    left = korely.get_facts(user_id=CUSTOMER, include_invalidated=True)
    print(f"read back with include_invalidated=true: {len(left)} facts")


if __name__ == "__main__":
    main()
