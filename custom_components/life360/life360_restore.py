#! /usr/bin/env python3.9

import argparse
import json
from pathlib import Path
import sys


storage_dir = Path(*Path(sys.argv[0]).parent.absolute().parts[:-2]) / ".storage"
DEFAULT_FILE = storage_dir / "life360_config_backup.json"


def main(args):
    backup_file = Path(args.backup_file)
    if not backup_file.exists():
        return f"{backup_file} does not exist"

    if not storage_dir.is_dir():
        return f"{storage_dir} does not exist"

    config_entries_file = storage_dir / "core.config_entries"
    if not config_entries_file.exists():
        return f"{config_entries_file} does not exist"

    entity_registry_file = storage_dir / "core.entity_registry"
    if not entity_registry_file.exists():
        return f"{entity_registry_file} does not exist"

    backup = json.load(backup_file.open())
    config_entries = json.load(config_entries_file.open())

    if (
        config_entries["version"] != backup["version"]
        or config_entries.get("minor_version") != backup["minor_version"]
    ):
        return (
            "core.config_entries version changed since backup: "
            f"{backup['version']}, {backup['minor_version']} -> "
            f"{config_entries['version']}, {config_entries['minor_version']}"
        )

    life360_entries = backup["life360_entries"]
    restored_entries = []
    for entry in config_entries["data"]["entries"]:
        if entry["domain"] != "life360":
            restored_entries.append(entry)
        elif entry["version"] == 1:
            return f"life360 config entries have not been migrated to version 2"
        elif not (backup_entry := life360_entries.get(entry["entry_id"])):
            if args.force:
                print(f"deleting new entry ({entry.unique_id}) that has no backup")
            else:
                return f"new entry ({entry.unique_id}) found that has no backup"
        else:
            restored_entries.append(backup_entry)
    config_entries["data"]["entries"] = restored_entries

    entity_registry = json.load(entity_registry_file.open())
    entity_registry["data"]["entities"] = list(
        filter(
            lambda x: x["platform"] != "life360", entity_registry["data"]["entities"]
        )
    )

    json.dump(config_entries, config_entries_file.open(mode="w"), indent=4)
    json.dump(entity_registry, entity_registry_file.open(mode="w"), indent=4)

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Restore Life360 config & registry entries"
    )
    parser.add_argument(
        "backup_file",
        nargs="?",
        default=DEFAULT_FILE,
        help=f"file to restore from (default: {DEFAULT_FILE})",
    )
    parser.add_argument(
        "-f", "--force", action="store_true", help="force delete new entries"
    )
    args = parser.parse_args()
    result = main(args)
    if result:
        parser.print_usage()
        result = f"error: {result}"
    sys.exit(result)
