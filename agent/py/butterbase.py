"""Thin client for the Butterbase auto-generated REST data API (PostgREST-style
filters, Bearer auth with the platform API key — this scraper is a trusted
local job running as the service role)."""
from __future__ import annotations

import datetime as dt

import requests

from uf_config import BUTTERBASE_API_BASE
from uf_env import env


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _request(method: str, path: str, params: dict | None = None, body: dict | None = None):
    resp = requests.request(
        method,
        f"{BUTTERBASE_API_BASE}{path}",
        params=params,
        json=body,
        headers={"Authorization": f"Bearer {env('BUTTERBASE_API_KEY')}"},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Butterbase {method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.status_code != 204 else None


def select(table: str, params: dict | None = None):
    return _request("GET", f"/{table}", params=params)


def insert(table: str, data: dict):
    return _request("POST", f"/{table}", body=data)


def update(table: str, row_id: str, data: dict):
    return _request("PATCH", f"/{table}/{row_id}", body=data)


# --- domain helpers ----------------------------------------------------------

def upsert_contact(platform: str, handle: str, display_name: str | None = None,
                   profile_url: str | None = None) -> dict:
    found = select("contacts", {"platform": f"eq.{platform}", "handle": f"eq.{handle}", "limit": 1})
    if found:
        c = found[0]
        patch = {"last_seen_at": _now_iso()}
        if display_name and not c.get("display_name"):
            patch["display_name"] = display_name
        update("contacts", c["id"], patch)
        return c
    return insert("contacts", {
        "platform": platform, "handle": handle,
        "display_name": display_name, "profile_url": profile_url,
    })


def item_exists(platform: str, external_id: str) -> bool:
    rows = select("items", {
        "platform": f"eq.{platform}", "external_id": f"eq.{external_id}",
        "select": "id", "limit": 1,
    })
    return len(rows) > 0


def upsert_item(item: dict) -> dict:
    """Insert, or update the existing row on (platform, external_id) — makes
    reprocessing (e.g. --overwrite-today after a schema change) idempotent."""
    found = select("items", {
        "platform": f"eq.{item['platform']}", "external_id": f"eq.{item['external_id']}",
        "limit": 1,
    })
    if found:
        row = found[0]
        patch = {k: v for k, v in item.items() if k not in ("platform", "external_id")}
        patch["graph_synced"] = False  # re-sync the graph with the fresh extraction
        update("items", row["id"], patch)
        return {**row, **patch}
    return insert("items", item)


def mark_graph_synced(item_id: str) -> None:
    update("items", item_id, {"graph_synced": True})


def start_run(platform: str) -> dict:
    return insert("scrape_runs", {"platform": platform, "status": "running"})


def finish_run(run_id: str, status: str, stats: dict, error: str | None = None) -> None:
    update("scrape_runs", run_id, {
        "status": status, "stats": stats, "error": error, "finished_at": _now_iso(),
    })


def last_completed_run(platform: str) -> dict | None:
    rows = select("scrape_runs", {
        "platform": f"eq.{platform}", "status": "eq.completed",
        "order": "started_at.desc", "limit": 1,
    })
    return rows[0] if rows else None
