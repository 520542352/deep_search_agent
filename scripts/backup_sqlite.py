from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.runtime import resolve_checkpoint_path  # noqa: E402
from persistence.database import resolve_database_path  # noqa: E402
from persistence.operations import (  # noqa: E402
    check_persistence_health,
    create_backup,
    restore_backup,
    verify_backup,
)


def _database_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--application-db",
        type=Path,
        default=resolve_database_path(PROJECT_ROOT, os.environ),
    )
    parser.add_argument(
        "--checkpoint-db",
        type=Path,
        default=resolve_checkpoint_path(PROJECT_ROOT, os.environ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Back up, verify, restore, and inspect Deep Search persistence."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup", help="Create a verified backup bundle.")
    _database_arguments(backup)
    backup.add_argument("--upload-root", type=Path, default=PROJECT_ROOT / "upload")
    backup.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "output")
    backup.add_argument(
        "--destination", type=Path, default=PROJECT_ROOT / "backups"
    )

    verify = commands.add_parser("verify", help="Verify a backup bundle.")
    verify.add_argument("bundle", type=Path)

    restore = commands.add_parser(
        "restore", help="Restore a bundle into a new or empty directory."
    )
    restore.add_argument("bundle", type=Path)
    restore.add_argument("target", type=Path)

    health = commands.add_parser("health", help="Check both SQLite databases.")
    _database_arguments(health)
    return parser


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    if arguments.command == "backup":
        bundle = create_backup(
            application_path=arguments.application_db,
            checkpoint_path=arguments.checkpoint_db,
            upload_root=arguments.upload_root,
            output_root=arguments.output_root,
            destination_root=arguments.destination,
        )
        _print_json({"status": "created", "bundle": str(bundle)})
        return 0
    if arguments.command == "verify":
        manifest = verify_backup(arguments.bundle)
        _print_json(
            {
                "status": "verified",
                "bundle": str(arguments.bundle),
                "created_at": manifest["created_at"],
                "file_count": len(manifest["files"]),
            }
        )
        return 0
    if arguments.command == "restore":
        target = restore_backup(arguments.bundle, arguments.target)
        _print_json({"status": "restored", "target": str(target)})
        return 0

    health = check_persistence_health(
        arguments.application_db,
        arguments.checkpoint_db,
    )
    _print_json(health.to_dict())
    return 0 if health.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
