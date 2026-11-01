"""Automatic data refresh: a polled delivery inbox and an optional synthetic feed.

A delivery is a data file plus a sidecar ``<file>.json`` manifest, for example
``source_a.csv`` and ``source_a.csv.json`` containing
``{"source": "A", "period": "2026-11", "expected_rows": 120, "complete": true}``.
Producers write the sidecar last (atomic rename). The worker picks up a pair
only after both have been unchanged for a few seconds.

Ingestion is content-addressed (batch id = source, period, file hash, sheet),
so re-polling or re-delivering an identical file is a replay, never a second
batch. A month close needs whole-period files: each delivery must contain every
record of the period so far, not only the new rows.
"""
from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import time

from .db import Blocked, Conflict, NotFound, audit, digest
from .models import RuntimeOption, now

ACTOR = "automatic-inbox"
FEED_ACTOR = "automatic-demo-feed"
INBOX_REPORT = "inbox.report"
FEED_STATE = "demo_feed.state"
SETTLE_SECONDS = 5
DATA_SUFFIXES = (".csv", ".parquet", ".xlsx")
_PERIOD = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
# The demo feed keeps only movements the logistic regression can actually
# score: no missing features, no quarantined rows and no knock-on cases.
FEED_EXCLUDED_SCENARIOS = frozenset({"MISSING_FEATURE", "INVALID_AMOUNT", "INVALID_TIMESTAMP", "CONFLICTING_DUPLICATE"})
FEED_CUSTOMERS = 8


def inbox_dir(settings) -> Path:
    return Path(settings.data_dir) / "inbox"


@contextmanager
def _exclusive(directory: Path):
    """One scanner per data directory; a busy lock skips this cycle."""
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)


def _manifest(path: Path) -> dict:
    raw = path.read_bytes()
    if len(raw) > 4096:
        raise ValueError("Sidecar manifest exceeds 4 KiB")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or set(value) - {"source", "period", "expected_rows", "complete", "sheet"}:
        raise ValueError("Sidecar must contain only source, period, expected_rows, complete and sheet")
    if value.get("source") not in ("A", "B") or not isinstance(value.get("period"), str) or not _PERIOD.fullmatch(value["period"]):
        raise ValueError("Sidecar needs source A or B and a YYYY-MM period")
    expected = value.get("expected_rows")
    if expected is not None and (type(expected) is not int or expected < 0):
        raise ValueError("expected_rows must be a nonnegative integer")
    if type(value.get("complete", False)) is not bool:
        raise ValueError("complete must be true or false")
    sheet = value.get("sheet")
    if sheet is not None and (not isinstance(sheet, str) or not 0 < len(sheet) <= 120):
        raise ValueError("sheet must be a short string")
    return {"source": value["source"], "period": value["period"], "expected_rows": expected,
            "complete": value.get("complete", False), "sheet": sheet}


def _ready_pairs(directory: Path, at: float) -> list[tuple[Path, Path]]:
    pairs = []
    for sidecar in sorted(directory.glob("*.json")):
        data = sidecar.with_suffix("")
        try:
            side_info, data_info = sidecar.lstat(), data.lstat()
        except FileNotFoundError:
            continue
        if not (stat.S_ISREG(side_info.st_mode) and stat.S_ISREG(data_info.st_mode)):
            continue  # Never follow links or ingest devices/directories.
        if data.suffix.lower() not in DATA_SUFFIXES:
            continue
        if at - max(side_info.st_mtime, data_info.st_mtime) < SETTLE_SECONDS:
            continue
        pairs.append((data, sidecar))
    return pairs


def _move(paths, target: Path, note: dict | None = None):
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for path in paths:
        if path.exists():
            os.replace(path, target / f"{stamp}-{path.name}")
    if note is not None:
        name = paths[0].name
        (target / f"{stamp}-{name}.result.json").write_text(json.dumps(note, indent=2, sort_keys=True, default=str) + "\n")


def _save(db, key: str, value: dict) -> None:
    """Store a report only when its content changes (no idle SSE churn)."""
    with db.transaction() as session:
        option = session.get(RuntimeOption, key)
        stored = {"checked_at": now().isoformat(), "report": value}
        if option is None:
            session.add(RuntimeOption(key=key, value=stored))
        elif option.value.get("report") != value:
            option.value = stored


def scan_inbox(db, *, at: float | None = None, max_files: int | None = None) -> dict:
    """Ingest at most ``max_files`` settled deliveries from ``var/inbox``.

    Success removes the data file (the content-addressed object store keeps
    the bytes) and records a small receipt in ``inbox/processed``. Invalid
    deliveries move to ``inbox/rejected`` with the reason. Queue backpressure
    leaves files in place for a later cycle.
    """
    from .ingestion import ingest_file
    from .operations import Backpressure
    settings = db.settings
    directory = inbox_dir(settings)
    limit = max_files or settings.inbox_max_files
    report = {"mode": "automatic_inbox", "ingested": 0, "replayed": 0, "rejected": 0, "deferred": 0, "results": []}
    with _exclusive(directory) as acquired:
        if not acquired:
            report["busy"] = True
            return report
        for data, sidecar in _ready_pairs(directory, time.time() if at is None else at)[:limit]:
            item = {"file": data.name}
            try:
                manifest = _manifest(sidecar)
                item.update(source=manifest["source"], period=manifest["period"])
                result = ingest_file(db, data, manifest["source"], manifest["period"], manifest["expected_rows"],
                                     manifest["complete"], manifest["sheet"], original_name=data.name, actor=ACTOR)
                item.update(status="REPLAYED" if result.get("replayed") else "INGESTED", batch_id=result["batch_id"],
                            rows=result.get("rows"))
                report["replayed" if result.get("replayed") else "ingested"] += 1
                note = {**item, "manifest": manifest}
                data.unlink(missing_ok=True)
                _move([sidecar], directory / "processed", note)
            except Backpressure as exc:
                item.update(status="DEFERRED", reason=str(exc)[:300])
                report["deferred"] += 1
                report["results"].append(item)
                break
            except (Conflict, Blocked, NotFound, ValueError, UnicodeError, json.JSONDecodeError) as exc:
                item.update(status="REJECTED", reason=str(exc)[:500])
                report["rejected"] += 1
                _move([data, sidecar], directory / "rejected", item)
                with db.transaction() as session:
                    audit(session, ACTOR, "inbox_file_rejected", digest(["inbox", data.name]), item)
            report["results"].append(item)
    if report["results"]:
        _save(db, INBOX_REPORT, report)
    return report


# --------------------------------------------------------------------------
# Synthetic demo feed: cumulative whole-month snapshots written to the inbox.

def _month_bounds(period: str):
    start = datetime.strptime(period, "%Y-%m").replace(tzinfo=timezone.utc)
    finish = (start + timedelta(days=32)).replace(day=1)
    return start, finish


def _previous(period: str) -> str:
    start, _ = _month_bounds(period)
    return (start - timedelta(days=1)).strftime("%Y-%m")


def _feed_dataset(settings, period: str) -> Path:
    from .synthetic import generate_dataset
    rows = settings.demo_feed_rows
    directory = Path(settings.data_dir) / "synthetic" / f"feed-{period}-{rows}"
    if not (directory / "manifest.json").is_file():
        # A fixed monthly total keeps every row's timestamp stable, so a later
        # snapshot only appends rows and never rewrites earlier ones.
        generate_dataset(directory, customers=FEED_CUSTOMERS, movements=rows,
                         seed=int(period.replace("-", "")), period=period)
    return directory


def _visible(directory: Path, source: str, cutoff: datetime | None, excluded: set[str]):
    with (directory / f"source_{source.lower()}.csv").open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for row in reader:
            if row["movement_id"] in excluded:
                continue
            if cutoff is not None and datetime.fromisoformat(row["event_at"]) > cutoff:
                continue
            rows.append(row)
        return rows, reader.fieldnames


def _write_delivery(directory: Path, name: str, fields, rows, manifest: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / ("." + name + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, directory / name)
    sidecar = directory / ("." + name + ".json.tmp")
    sidecar.write_text(json.dumps(manifest, sort_keys=True))
    os.replace(sidecar, directory / (name + ".json"))  # Sidecar last: the pair is now visible.


def feed_tick(db, at: datetime | None = None) -> dict:
    """Write new cumulative A/B snapshots when more synthetic rows are 'due'.

    Rows become visible when their event time passes. When a month ends, one
    final full snapshot is written so that month can close automatically.
    """
    from .ingestion import load_customers
    settings = db.settings
    if not settings.synthetic:
        raise Blocked("The demo feed runs only in synthetic mode")
    at = at or datetime.now(timezone.utc)
    current = at.astimezone(timezone.utc).strftime("%Y-%m")
    with db.transaction() as session:
        option = session.get(RuntimeOption, FEED_STATE)
        state = dict(option.value) if option else {}
    periods = [current]
    previous = _previous(current)
    if previous in state and not state[previous].get("final"):
        periods.insert(0, previous)
    report = {"mode": "synthetic_demo_feed", "written": [], "unchanged": []}
    for period in periods:
        directory = _feed_dataset(settings, period)
        entry = dict(state.get(period, {}))
        if not entry.get("customers_loaded"):
            load_customers(db, directory / "customers.json")
            entry["customers_loaded"] = True
        excluded = set()
        with (directory / "scenario_labels.csv").open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row["scenario"] in FEED_EXCLUDED_SCENARIOS:
                    excluded.add(row["movement_id"])
        _, finish = _month_bounds(period)
        final = at >= finish
        cutoff = None if final else at
        snapshot = {}
        for source in ("A", "B"):
            snapshot[source] = _visible(directory, source, cutoff, excluded)
        counts = {source: len(rows) for source, (rows, _) in snapshot.items()}
        if not counts["A"] or not counts["B"] or counts == entry.get("counts"):
            # Rows only ever become visible, so equal counts mean the same
            # snapshot: an already-written full snapshot is the final one.
            if final and counts == entry.get("counts"):
                entry["final"] = True
            state[period] = entry
            report["unchanged"].append({"period": period, "counts": counts})
            continue
        sequence = int(entry.get("sequence", 0)) + 1
        for source, (rows, fields) in snapshot.items():
            name = f"feed-{period}-{sequence:06d}-{source}.csv"
            _write_delivery(inbox_dir(settings), name, fields, rows,
                            {"source": source, "period": period, "expected_rows": len(rows), "complete": True})
        state[period] = {**entry, "sequence": sequence, "counts": counts, "final": final, "written_at": at.isoformat()}
        report["written"].append({"period": period, "sequence": sequence, "counts": counts, "final": final})
    # Keep a bounded state: the current and previous month only.
    state = {key: value for key, value in state.items() if key in (current, previous)}
    with db.transaction() as session:
        option = session.get(RuntimeOption, FEED_STATE)
        if option is None:
            session.add(RuntimeOption(key=FEED_STATE, value=state))
        else:
            option.value = state
        if report["written"]:
            audit(session, FEED_ACTOR, "inbox_feed_snapshot", FEED_STATE, report)
    return report


def run_refresh_cycle(db, last_feed: float | None, clock=time.monotonic) -> tuple[dict, float | None]:
    """One data-refresh cycle for the worker thread; returns (report, last_feed)."""
    from .operations import get_effective_automation
    with db.transaction() as session:
        control = get_effective_automation(session, db.settings)
    if not control["processing_enabled"]:
        return {"paused": True}, last_feed
    result = {}
    seconds = db.settings.demo_feed_seconds
    if seconds and (last_feed is None or clock() - last_feed >= seconds):
        result["feed"] = feed_tick(db)
        last_feed = clock()
    result["inbox"] = scan_inbox(db)
    return result, last_feed
