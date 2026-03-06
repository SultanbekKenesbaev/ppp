#!/usr/bin/env python3
"""One-time SQLite -> PostgreSQL migration utility for TaskPlatform."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import db  # noqa: E402
from app import models as _models  # noqa: F401,E402  # ensures metadata is loaded

TABLE_ORDER = [
    "districts",
    "mahallas",
    "streets",
    "users",
    "worker_assignments",
    "conversations",
    "conversation_members",
    "messages",
    "attachments",
    "device_tokens",
    "task_batches",
    "task_batch_recipients",
]

BATCH_SIZE = 1000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate TaskPlatform data from SQLite to PostgreSQL")
    parser.add_argument(
        "--src-sqlite",
        default=os.getenv("SRC_SQLITE_PATH", str(ROOT_DIR / "app.db")),
        help="Path to source SQLite DB (default: env SRC_SQLITE_PATH or ./app.db)",
    )
    parser.add_argument(
        "--dst-url",
        default=os.getenv("DST_DATABASE_URL", ""),
        help="Destination PostgreSQL SQLAlchemy URL (default: env DST_DATABASE_URL)",
    )
    parser.add_argument(
        "--drop-existing",
        action="store_true",
        help="Drop destination schema before migration",
    )
    return parser.parse_args()


def table_counts(engine: Engine) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    with engine.connect() as conn:
        for table_name in TABLE_ORDER:
            table = db.metadata.tables[table_name]
            counts[table_name] = conn.execute(select(func.count()).select_from(table)).scalar_one()
    return counts


def ensure_destination_ready(dst_engine: Engine, drop_existing: bool) -> None:
    if drop_existing:
        print("[migrate] Dropping destination schema...")
        db.metadata.drop_all(dst_engine)
        db.metadata.create_all(dst_engine)
        return

    db.metadata.create_all(dst_engine)
    existing_counts = table_counts(dst_engine)
    non_empty = {k: v for k, v in existing_counts.items() if v > 0}
    if non_empty:
        details = ", ".join(f"{name}={count}" for name, count in non_empty.items())
        raise RuntimeError(
            "Destination database is not empty. "
            f"Found rows: {details}. Re-run with --drop-existing if this is intentional."
        )


def copy_table(src_engine: Engine, dst_engine: Engine, table_name: str) -> int:
    table = db.metadata.tables[table_name]
    copied = 0
    with src_engine.connect() as src_conn, dst_engine.begin() as dst_conn:
        result = src_conn.execute(select(table))
        while True:
            batch = result.fetchmany(BATCH_SIZE)
            if not batch:
                break
            rows = [dict(row._mapping) for row in batch]
            dst_conn.execute(table.insert(), rows)
            copied += len(rows)
    return copied


def reset_sequences(dst_engine: Engine) -> None:
    with dst_engine.begin() as conn:
        for table_name in TABLE_ORDER:
            table = db.metadata.tables[table_name]
            if "id" not in table.c:
                continue
            max_id = conn.execute(text(f'SELECT COALESCE(MAX(id), 0) FROM "{table_name}"')).scalar_one()
            if max_id <= 0:
                conn.execute(
                    text("SELECT setval(pg_get_serial_sequence(:table_name, 'id'), 1, false)"),
                    {"table_name": table_name},
                )
            else:
                conn.execute(
                    text("SELECT setval(pg_get_serial_sequence(:table_name, 'id'), :value, true)"),
                    {"table_name": table_name, "value": int(max_id)},
                )


def main() -> int:
    args = parse_args()

    src_path = Path(args.src_sqlite).expanduser().resolve()
    if not src_path.exists():
        print(f"ERROR: source SQLite file does not exist: {src_path}", file=sys.stderr)
        return 1

    dst_url = args.dst_url.strip()
    if not dst_url:
        print("ERROR: destination URL is required via --dst-url or DST_DATABASE_URL", file=sys.stderr)
        return 1
    if not dst_url.startswith("postgresql"):
        print("ERROR: destination URL must start with 'postgresql'", file=sys.stderr)
        return 1

    src_engine = create_engine(f"sqlite:///{src_path}")
    dst_engine = create_engine(dst_url)

    try:
        print(f"[migrate] Source: {src_path}")
        print(f"[migrate] Destination: {dst_url.split('@')[-1]}")
        ensure_destination_ready(dst_engine, args.drop_existing)

        print("[migrate] Copying tables...")
        copied_counts: Dict[str, int] = {}
        for table_name in TABLE_ORDER:
            copied = copy_table(src_engine, dst_engine, table_name)
            copied_counts[table_name] = copied
            print(f"  - {table_name}: {copied}")

        print("[migrate] Resetting PostgreSQL sequences...")
        reset_sequences(dst_engine)

        print("[migrate] Validating row counts...")
        src_counts = table_counts(src_engine)
        dst_counts = table_counts(dst_engine)

        mismatches = []
        for table_name in TABLE_ORDER:
            src_count = src_counts[table_name]
            dst_count = dst_counts[table_name]
            status = "OK" if src_count == dst_count else "MISMATCH"
            print(f"  - {table_name}: src={src_count}, dst={dst_count} [{status}]")
            if src_count != dst_count:
                mismatches.append(table_name)

        if mismatches:
            print(
                "ERROR: row count mismatch in tables: " + ", ".join(mismatches),
                file=sys.stderr,
            )
            return 1

        print("[migrate] Migration completed successfully.")
        return 0
    finally:
        src_engine.dispose()
        dst_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
