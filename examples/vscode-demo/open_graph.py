"""Open the dashboard's graph on the demo customer, at a date.

    python open_graph.py [--as-of 2026-04-01 | --now]

The graph page reads `user_id` and `as_of` from its address, so this opens it
already showing what the store believed that day, with the slider there to
drag forward to June. Log in once first (the login is in .demo-login).
"""
import argparse
import webbrowser

from _demo import CUSTOMER, SCREENSHOT_DAY, settings


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=SCREENSHOT_DAY)
    ap.add_argument("--now", action="store_true", help="today, with the history loaded")
    a = ap.parse_args()
    base = settings().get("KORELY_BASE_URL", "").rstrip("/")
    if not base:
        raise SystemExit("No address yet: run the task 'Korely 2: a key' first.")
    query = f"user_id={CUSTOMER}&" + ("history=1" if a.now else f"as_of={a.as_of}")
    url = f"{base}/graph?{query}"
    print(url)
    webbrowser.open(url)


if __name__ == "__main__":
    main()
