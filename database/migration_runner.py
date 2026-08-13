"""Run schema migrations against an update candidate database only."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Sequence


def _resolve_candidate(project_root: Path, database_path: Path) -> Path:
    root = project_root.resolve()
    backup_root = (root / "backup").resolve()
    candidate = database_path.resolve()
    if (root / "backup").is_symlink():
        raise RuntimeError("backup must not be a symbolic link")
    try:
        candidate.relative_to(backup_root)
    except ValueError as exc:
        raise RuntimeError("migration candidate must be located inside backup") from exc
    live_database = (root / "database" / "vpn_bot.db").resolve()
    if candidate == live_database:
        raise RuntimeError("refusing to migrate the live database")
    if not candidate.is_file() or candidate.is_symlink():
        raise RuntimeError("migration candidate is missing or is a symbolic link")
    return candidate


def _validate_database(path: Path) -> None:
    connection = sqlite3.connect(str(path), timeout=30)
    try:
        quick_rows = connection.execute("PRAGMA quick_check").fetchall()
        if len(quick_rows) != 1 or quick_rows[0][0] != "ok":
            raise RuntimeError(f"quick_check failed: {quick_rows[:5]}")
        foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_rows:
            raise RuntimeError(f"foreign_key_check failed: {foreign_key_rows[:5]}")
    finally:
        connection.close()


def _require_complete_wal_checkpoint(
    row: Sequence[object] | None,
) -> tuple[int, int, int]:
    """Validate the complete result contract of a TRUNCATE checkpoint."""
    if (
        row is None
        or len(row) != 3
        or any(type(value) is not int for value in row)
    ):
        raise RuntimeError(
            "candidate WAL checkpoint(TRUNCATE) returned an invalid result: "
            f"result={row!r}"
        )

    busy, log_frames, checkpointed_frames = (
        int(row[0]),
        int(row[1]),
        int(row[2]),
    )
    details = (
        f"busy={busy}, log_frames={log_frames}, "
        f"checkpointed_frames={checkpointed_frames}"
    )
    if busy == 1:
        raise RuntimeError(
            "candidate WAL checkpoint(TRUNCATE) did not complete: " + details
        )
    if busy != 0:
        raise RuntimeError(
            "candidate WAL checkpoint(TRUNCATE) returned an invalid result: "
            + details
        )
    if (log_frames, checkpointed_frames) not in {(0, 0), (-1, -1)}:
        raise RuntimeError(
            "candidate WAL checkpoint(TRUNCATE) returned an incomplete result: "
            + details
        )
    return busy, log_frames, checkpointed_frames


def _checkpoint_details(result: tuple[int, int, int] | None) -> str:
    if result is None:
        return (
            "busy=<unavailable>, log_frames=<unavailable>, "
            "checkpointed_frames=<unavailable>"
        )
    busy, log_frames, checkpointed_frames = result
    return (
        f"busy={busy}, log_frames={log_frames}, "
        f"checkpointed_frames={checkpointed_frames}"
    )


def _finalize_candidate_file(path: Path) -> None:
    """Checkpoint WAL data so the candidate is a single movable SQLite file."""
    checkpoint_result: tuple[int, int, int] | None = None
    try:
        connection = sqlite3.connect(str(path), timeout=30)
    except sqlite3.Error as exc:
        raise RuntimeError(
            "opening candidate for WAL finalization failed "
            f"({_checkpoint_details(checkpoint_result)}): {exc}"
        ) from exc

    try:
        try:
            checkpoint_cursor = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            )
            try:
                checkpoint_row = checkpoint_cursor.fetchone()
            finally:
                checkpoint_cursor.close()
        except sqlite3.Error as exc:
            raise RuntimeError(
                "candidate WAL checkpoint(TRUNCATE) execution failed "
                f"({_checkpoint_details(checkpoint_result)}): {exc}"
            ) from exc
        checkpoint_result = _require_complete_wal_checkpoint(checkpoint_row)

        try:
            journal_mode_cursor = connection.execute(
                "PRAGMA journal_mode = DELETE"
            )
            try:
                journal_mode = journal_mode_cursor.fetchone()
            finally:
                journal_mode_cursor.close()
        except sqlite3.Error as exc:
            raise RuntimeError(
                "candidate journal_mode=DELETE transition failed after checkpoint "
                f"({_checkpoint_details(checkpoint_result)}): {exc}"
            ) from exc
        if not journal_mode or str(journal_mode[0]).lower() != "delete":
            raise RuntimeError(
                "candidate journal_mode=DELETE transition returned an invalid result "
                f"({_checkpoint_details(checkpoint_result)}): result={journal_mode!r}"
            )
    finally:
        connection.close()

    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        sidecar_size = sidecar.stat().st_size if sidecar.exists() else 0
        if sidecar_size > 0:
            raise RuntimeError(
                "candidate sidecar remained non-empty after WAL finalization "
                f"({_checkpoint_details(checkpoint_result)}): "
                f"{sidecar.name}={sidecar_size} bytes"
            )
        sidecar.unlink(missing_ok=True)


def _read_schema_version(path: Path) -> int:
    connection = sqlite3.connect(str(path), timeout=30)
    try:
        row = connection.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()


def run_candidate_migrations(project_root: Path, database_path: Path) -> int:
    """Migrate one validated candidate and verify the resulting schema."""
    candidate = _resolve_candidate(project_root, database_path)
    _validate_database(candidate)

    from database import connection as db_connection

    db_connection.DB_PATH = candidate
    from database import migrations

    migrations.run_migrations()
    _finalize_candidate_file(candidate)
    _validate_database(candidate)
    version = _read_schema_version(candidate)
    if version != migrations.LATEST_VERSION:
        raise RuntimeError(
            f"schema version mismatch: expected {migrations.LATEST_VERSION}, got {version}"
        )
    print(
        json.dumps(
            {
                "status": "ok",
                "schema_version": version,
                "database": str(candidate),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate a staged YadrenoVPN database")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--database", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return run_candidate_migrations(
            Path(args.project_root),
            Path(args.database),
        )
    except Exception as exc:
        print(f"Migration candidate failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
