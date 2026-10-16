"""Create a fresh fictional demonstration match through the public API."""

from __future__ import annotations

import argparse
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="Backend origin")
    parser.add_argument("--name", default="Demonstration match")
    parser.add_argument("--home", default="Milton Keynes Storm")
    parser.add_argument("--away", default="Northbank Wolves")
    args = parser.parse_args()
    headers = {"Content-Type": "application/json"}
    if token := os.getenv("TOI_AUTH_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"{args.url.rstrip('/')}/api/v1/games",
        data=json.dumps({"name": args.name, "home_name": args.home, "away_name": args.away}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            state = json.load(response)
    except HTTPError as error:
        raise SystemExit(f"Could not seed match (HTTP {error.code}): {error.read().decode()}") from error
    print(f"Created {state['game']['id']}: {state['game']['name']}")


if __name__ == "__main__":
    main()
