"""Supply more synthetic inputs, or collect scheduler-produced invoice evidence."""
import argparse
import json
from pathlib import Path

from invoice_optimizer.config import Settings
from invoice_optimizer.db import Database
from invoice_optimizer.seed_invoice_examples import collect_results, seed_data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("ingest", "collect"))
    parser.add_argument("--output", type=Path, default=Path("output/invoice-examples"))
    args = parser.parse_args()
    db = Database(Settings.from_env().prepare())
    try:
        result = seed_data(db) if args.command == "ingest" else collect_results(db, args.output)
        print(json.dumps(result, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
