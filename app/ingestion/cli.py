import argparse
import json
import sys

from app.db import SessionLocal
from app.ingestion.sync import sync_inventory


def main() -> None:
    parser = argparse.ArgumentParser(description="Property inventory synchronization CLI")
    parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Path to inventory CSV file (defaults to INVENTORY_PATH setting)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate synchronization without committing changes",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Force sync even if file sha256 matches previous run",
    )

    args = parser.parse_args()

    with SessionLocal() as session:
        try:
            summary = sync_inventory(
                session=session,
                file_path=args.path,
                dry_run=args.dry_run,
                force=args.force,
            )
            print(json.dumps(summary, indent=2))
            if summary.get("status") in ("failed", "aborted"):
                sys.exit(1)
        except Exception as e:
            print(f"Sync execution error: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
