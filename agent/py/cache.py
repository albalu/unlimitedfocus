"""Daily file cache — scraped data lands on disk BEFORE any DB commit, so
reruns are safe and captures survive crashes / schema changes / extraction
failures (stories especially: they expire from Instagram within 24h, but their
capture lives here and can be re-processed any time this week).

Layout:
    agent/data/cache/YYYY-MM-DD/captured.jsonl   full capture records (append-only)
    agent/data/cache/YYYY-MM-DD/committed.txt    external_ids whose DB+graph write succeeded

Both files are shared by all platforms: capture records carry a "platform"
field and committed lines are "<platform>:<external_id>", so each scraper only
sees (and commits) its own items. Lines/records written before the cache was
platform-aware lack the tag and are read as instagram's.

Lifecycle: a run appends to captured.jsonl during the walk phases; the process
phase extracts + upserts each captured-but-not-committed record and then marks
it committed. Directories older than PURGE_DAYS are deleted at run start.
Media files under data/media/ are referenced by records but not purged here —
TODO(P1): move media to Butterbase storage, then purge local copies with cache.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil

from uf_env import DATA_DIR

CACHE_DIR = DATA_DIR / "cache"
PURGE_DAYS = 7
_LEGACY_PLATFORM = "instagram"  # pre-tag records/lines belong to instagram


def _today() -> str:
    return dt.date.today().isoformat()


def today_dir():
    d = CACHE_DIR / _today()
    d.mkdir(parents=True, exist_ok=True)
    return d


def purge_old(days: int = PURGE_DAYS) -> int:
    """Delete cache dirs older than `days`. Returns how many were removed."""
    if not CACHE_DIR.exists():
        return 0
    cutoff = dt.date.today() - dt.timedelta(days=days)
    removed = 0
    for child in CACHE_DIR.iterdir():
        if not child.is_dir():
            continue
        try:
            day = dt.date.fromisoformat(child.name)
        except ValueError:
            continue  # not a date-named dir — leave it alone
        if day < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


def load_captured(platform: str) -> dict[str, dict]:
    """Today's capture records for one platform, keyed by external_id
    (last write wins)."""
    path = today_dir() / "captured.jsonl"
    records: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("platform", _LEGACY_PLATFORM) == platform:
                    records[rec["external_id"]] = rec
            except Exception:
                continue  # torn line from a crash — the item just re-captures
    return records


def append_captured(rec: dict) -> None:
    with open(today_dir() / "captured.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_committed(platform: str) -> set[str]:
    path = today_dir() / "committed.txt"
    if not path.exists():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        prefix, sep, ext_id = line.partition(":")
        if sep and prefix == platform:
            out.add(ext_id)
        elif not sep and platform == _LEGACY_PLATFORM:
            out.add(line)
    return out


def mark_committed(platform: str, external_id: str) -> None:
    with open(today_dir() / "committed.txt", "a", encoding="utf-8") as fh:
        fh.write(f"{platform}:{external_id}\n")
