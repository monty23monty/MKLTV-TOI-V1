"""Offline maintenance after restoring a database; never runs an API worker."""

import argparse
import json
import sys

from sqlalchemy.exc import SQLAlchemyError

from .database import database_url
from .store import Store, StoreError
from .version import build_metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    rotate = commands.add_parser("rotate-journals", help="Fence external clocks and invalidate pre-restore service cursors")
    targets = rotate.add_mutually_exclusive_group(required=True)
    targets.add_argument("--all", action="store_true", help="Rotate all restored games")
    targets.add_argument("--game-id", help="Rotate one restored game")
    rotate.add_argument("--confirm-offline", action="store_true", required=True,
                        help="Confirm every API worker is stopped before maintenance")
    args = parser.parse_args(argv)
    store = None
    try:
        store = Store(database_url())
        store.migrate()
        games = [row["id"] for row in store.list_games()] if args.all else [args.game_id]
        results = [{"game_id": game_id, **store.rotate_journal(game_id)} for game_id in games]
        print(json.dumps({"operation": "rotate-journals", "games": results, **build_metadata()}))
        return 0
    except StoreError as error:
        print(f"Maintenance rejected: {error}", file=sys.stderr)
        return 1
    except (SQLAlchemyError, ValueError):
        print("Maintenance failed. Check database availability and configuration; no credentials are printed.", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
