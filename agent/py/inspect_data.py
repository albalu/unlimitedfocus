#!/usr/bin/env python3
"""Quick look at what the scraper has collected so far (reads Butterbase).
Usage: uv run inspect_data.py"""
from __future__ import annotations

import butterbase as bb

runs = bb.select("scrape_runs", {"order": "started_at.desc", "limit": 5})
contacts = bb.select("contacts", {"order": "last_seen_at.desc", "limit": 100})
items = bb.select("items", {"order": "captured_at.desc", "limit": 15})

print("── recent runs ─────────────────────────────")
for r in runs:
    print(f"  {r['started_at']}  {r['platform']}  {r['status']}  {r.get('stats')}")
if not runs:
    print("  (none yet)")

print(f"\n── contacts ({len(contacts)}) ──────────────────────")
for c in contacts[:25]:
    dn = f" ({c['display_name']})" if c.get("display_name") else ""
    print(f"  @{c['handle']}{dn}  last seen {c['last_seen_at']}")

print("\n── extraction feedback (🚩 flags to act on) ─")
flags = bb.select("extraction_feedback", {"order": "created_at.desc", "limit": 10})
for f in flags:
    snap = f.get("item_snapshot") or {}
    print(f"  {f['created_at']}  @{snap.get('handle')}  [{snap.get('kind')}] {snap.get('url')}")
    print(f"     said: {snap.get('brief')}")
    print(f"     🚩 user: {f['feedback']}")
if not flags:
    print("  (none yet)")

print("\n── latest items ────────────────────────────")
for i in items:
    print(f"  [{i['kind']}] {i.get('topic') or '—'}  {i.get('posted_at') or i['captured_at']}")
    print(f"     {i.get('brief') or '(no brief)'}")
    print(f"     {i.get('url')}  graph_synced={i.get('graph_synced')}")
if not items:
    print("  (none yet — run: uv run scrape_instagram.py)")
