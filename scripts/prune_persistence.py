from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.runtime import resolve_checkpoint_path  # noqa: E402
from persistence.database import resolve_database_path  # noqa: E402
from persistence.operations import (  # noqa: E402
    PersistenceOperationError,
    prune_archived_threads,
)


def _positive_days(value: str) -> int:
    days = int(value)
    if days <= 0:
        raise argparse.ArgumentTypeError("retention days must be greater than zero")
    return days


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or delete archived threads older than the retention period. "
            "The default is a dry run."
        )
    )
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
    parser.add_argument("--upload-root", type=Path, default=PROJECT_ROOT / "upload")
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "output")
    parser.add_argument("--older-than-days", type=_positive_days, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform deletion. Without this flag the command only previews changes.",
    )
    return parser


def _assert_no_wal_sidecars(paths: tuple[Path, ...]) -> None:
    sidecars = [
        Path(f"{path}{suffix}")
        for path in paths
        for suffix in ("-wal", "-shm")
        if Path(f"{path}{suffix}").exists()
    ]
    if sidecars:
        joined = ", ".join(str(path) for path in sidecars)
        raise PersistenceOperationError(
            "refusing to prune while SQLite WAL sidecars exist; stop the API "
            f"service and close database clients first: {joined}"
        )


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    if arguments.apply:
        _assert_no_wal_sidecars(
            (arguments.application_db, arguments.checkpoint_db)
        )
    cutoff = datetime.now(UTC) - timedelta(days=arguments.older_than_days)
    result = prune_archived_threads(
        application_path=arguments.application_db,
        checkpoint_path=arguments.checkpoint_db,
        upload_root=arguments.upload_root,
        output_root=arguments.output_root,
        older_than=cutoff,
        apply=arguments.apply,
    )
    print(
        json.dumps(
            result.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
