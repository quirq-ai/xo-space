#!/usr/bin/env python3
"""Rehearse this build's store upgrade on a copy of a real store.

For each store given, it makes a copy with SQLite's backup API (safe while
XO Space runs; the source is opened read-only and never written) in
``/tmp/storm-build/rehearse-<UTC stamp>/``, opens the copy with this build's
``Store.open``, which migrates an older store in place or refuses it, and
reports:
- the schema before and after, and what happened: ``current`` (already this
  build's), ``migrated``, ``refused`` or ``failed`` (with the store's own
  message), or ``crashed`` (anything else: a bug, since an upgrade must never
  crash the boot);
- how long the open took;
- each table's rows before and after (every table the store had must keep
  every row);
- SQLite's ``integrity_check`` and ``foreign_key_check`` on the result.

    venv/bin/python scripts/storm_rehearse_upgrade.py ~/.quirq/engine/xo.db ~/.quirq/engine/xo.db.bak-*
    venv/bin/python scripts/storm_rehearse_upgrade.py STORE --table      # for people

It exits 1 when any rehearsal isn't clean: refused, failed or crashed, a
row lost, or a finding.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
import traceback
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))       # run as `python scripts/storm_rehearse_upgrade.py`: the repo's packages resolve

with warnings.catch_warnings():     # importing the store resolves the active agent, which may warn
    warnings.simplefilter("ignore")
    from services.cowork_agent.storm.store import SCHEMA_VERSION, Store, StoreFailed, StoreRefused  # noqa: E402

WORK = Path("/tmp/storm-build")


def backup(source: Path, copy: Path) -> float:
    """Copy ``source`` page by page into ``copy``; returns the seconds it took."""
    started = time.monotonic()
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return time.monotonic() - started


def facts(path: Path) -> dict[str, Any]:
    """The copy's version and each table's rows, read-only."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {"version": conn.execute("PRAGMA user_version").fetchone()[0],
                "rows": {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}}
    finally:
        conn.close()


def checks(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check(20)")]
        broken: dict[str, int] = {}
        for table, _rowid, parent, _key in conn.execute("PRAGMA foreign_key_check"):
            broken[f"{table} -> {parent}"] = broken.get(f"{table} -> {parent}", 0) + 1
        return {"integrity_check": integrity, "foreign_key_check": broken}
    finally:
        conn.close()


async def open_copy(copy: Path) -> tuple[str, str | None]:
    try:
        store = await Store.open(copy)
    except StoreRefused as exc:
        return "refused", exc.message
    except StoreFailed as exc:
        return "failed", exc.message
    except Exception:  # noqa: BLE001 - the rehearsal reports a crash instead of being one
        return "crashed", traceback.format_exc()
    await store.close()
    return "opened", None


def rehearse(source: Path, copy: Path) -> dict[str, Any]:
    report: dict[str, Any] = {"source": str(source), "copy": str(copy), "this_build_schema": SCHEMA_VERSION}
    report["backup_s"] = round(backup(source, copy), 2)
    before = facts(copy)
    started = time.monotonic()
    outcome, message = asyncio.run(open_copy(copy))
    report["open_s"] = round(time.monotonic() - started, 2)
    after = facts(copy)
    if outcome == "opened":
        outcome = "migrated" if after["version"] != before["version"] else "current"
    lost = {t: n - after["rows"].get(t, 0) for t, n in before["rows"].items() if after["rows"].get(t, 0) < n}
    report.update({
        "from_schema": before["version"], "to_schema": after["version"], "outcome": outcome, "message": message,
        "rows_before": before["rows"], "rows_after": after["rows"], "rows_lost": lost,
        "tables_added": sorted(set(after["rows"]) - set(before["rows"])),
        **checks(copy),
    })
    report["clean"] = (outcome in ("current", "migrated") and not lost and report["integrity_check"] == ["ok"]
                       and not report["foreign_key_check"])
    return report


def describe(report: dict[str, Any]) -> list[str]:
    if report["outcome"] == "unreadable":
        return [f"{report['source']}", f"  unreadable: {report['message']}"]
    lines = [f"{report['source']}",
             f"  copy: {report['copy']} (backup {report['backup_s']} s)",
             f"  schema {report['from_schema']} -> {report['to_schema']} (this build: {report['this_build_schema']}):"
             f" {report['outcome']} in {report['open_s']} s" + ("" if report["clean"] else "  NOT CLEAN")]
    if report["message"]:
        lines.append(f"  message: {report['message'].strip()}")
    lines.append(f"  rows: {sum(report['rows_before'].values())} before, {sum(report['rows_after'].values())} after"
                 f" in {len(report['rows_after'])} tables; lost: {report['rows_lost'] or 'none'}; tables added: "
                 f"{', '.join(report['tables_added']) or 'none'}")
    lines.append(f"  integrity_check: {'; '.join(report['integrity_check'])}; foreign_key_check: "
                 f"{report['foreign_key_check'] or 'nothing'}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stores", type=Path, nargs="+", help="stores to copy and rehearse on (each is only read)")
    parser.add_argument("--work", type=Path, default=WORK, help=f"where the copies go (default {WORK})")
    parser.add_argument("--table", action="store_true", help="a report for people instead of JSON")
    args = parser.parse_args(argv)
    for store in args.stores:
        if not store.is_file():
            parser.error(f"{store} isn't a file")
    folder = args.work / f"rehearse-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    folder.mkdir(parents=True, exist_ok=False)
    reports = []
    for n, store in enumerate(args.stores, 1):
        try:
            reports.append(rehearse(store, folder / f"{n:02d}-{store.name.removesuffix('.db')}.db"))
        except sqlite3.Error as exc:          # not a database SQLite can read: said, and the others still run
            reports.append({"source": str(store), "outcome": "unreadable", "message": str(exc), "clean": False})
    if args.table:
        print("\n".join(line for report in reports for line in describe(report)))
    else:
        print(json.dumps(reports, indent=2))
    return 0 if all(report["clean"] for report in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
