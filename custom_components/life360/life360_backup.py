#! /usr/bin/env python3.9

import argparse
import json
from pathlib import Path
import sys


storage_dir = Path(*Path(sys.argv[0]).parent.absolute().parts[:-2]) / ".storage"
DEFAULT_FILE = storage_dir / "life360_config_backup.json"


def main(args):
    backup_file = Path(args.backup_file)
    if backup_file.exists() and not args.force:
        return f"{backup_file} already exists"

    if not storage_dir.is_dir():
        return f"{storage_dir} does not exist"

    config_entries_file = storage_dir / "core.config_entries"
    if not config_entries_file.exists():
        return f"{config_entries_file} does not exist"

    config_entries = json.load(config_entries_file.open())

    life360_entries = list(
        filter(lambda x: x["domain"] == "life360", config_entries["data"]["entries"])
    )
    if any(entry["version"] != 1 for entry in life360_entries):
        return "life360 config entries not all version 1"

    json.dump(
        {
            "version": config_entries["version"],
            "minor_version": config_entries.get("minor_version"),
            "life360_entries": {
                entry["entry_id"]: entry for entry in life360_entries
            },
        },
        backup_file.open(mode="w"),
        indent=4
    )

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backup Life360 config entries")
    parser.add_argument(
        "backup_file",
        nargs="?",
        default=DEFAULT_FILE,
        help=f"file to write backup into (default: {DEFAULT_FILE})",
    )
    parser.add_argument("-f", "--force", action="store_true", help="force overwrite")
    args = parser.parse_args()
    result = main(args)
    if result:
        parser.print_usage()
        result = f"error: {result}"
    sys.exit(result)
