#!/usr/bin/env python3
"""Re-sync items into Neo4j that were captured while the graph was unreachable
(or during a --no-graph run). Idempotent (MERGE-based), safe to rerun.
Usage: uv run graph_sync.py"""
from __future__ import annotations

import butterbase as bb
import graph

graph.verify_graph()
graph.ensure_constraints()
print("neo4j connected")

pending = bb.select("items", {"graph_synced": "eq.false", "order": "captured_at.asc", "limit": 500})
print(f"{len(pending)} item(s) pending graph sync")

synced = 0
try:
    for item in pending:
        if item.get("contact_id"):
            rows = bb.select("contacts", {"id": f"eq.{item['contact_id']}", "limit": 1})
            contact = rows[0] if rows else {"handle": "unknown", "display_name": None}
        else:
            contact = {"handle": "unknown", "display_name": None}
        mentions = (item.get("structured") or {}).get("mentions") or []
        graph.sync_item_to_graph(contact, item, mentions)
        bb.mark_graph_synced(item["id"])
        synced += 1
finally:
    graph.close_graph()

print(f"synced {synced}/{len(pending)}")
