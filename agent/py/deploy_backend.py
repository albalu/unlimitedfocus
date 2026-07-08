#!/usr/bin/env python3
"""Deploy the Butterbase backend: schema, serverless functions, CORS, and
(optionally) the payments plan. Idempotent — rerun after any change.

Runs locally with the repo-root .env (same trust boundary as the scraper):
secrets never leave this machine except to Butterbase itself.

Usage:
    uv run deploy_backend.py               # schema + functions + CORS + smoke test
    uv run deploy_backend.py --billing     # also: Stripe Connect onboarding + "Focus Pro" plan
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import secrets as pysecrets
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

import uf_config as cfg
from uf_env import AGENT_ROOT, REPO_ROOT, env

API = "https://api.butterbase.ai"
APP = cfg.BUTTERBASE_APP_ID
FRONTEND_URL = "https://unlimitedfocus.butterbase.dev"
FUNCTIONS_DIR = AGENT_ROOT / "backend" / "functions"
SCHEMA_FILE = AGENT_ROOT / "backend" / "schema.json"


def hdrs() -> dict:
    return {"Authorization": f"Bearer {env('BUTTERBASE_API_KEY')}", "Content-Type": "application/json"}


def call(method: str, path: str, body: dict | None = None, ok=(200, 201)) -> dict:
    r = requests.request(method, f"{API}{path}", json=body, headers=hdrs(), timeout=120)
    if r.status_code not in ok:
        raise SystemExit(f"{method} {path} -> HTTP {r.status_code}: {r.text[:400]}")
    return r.json() if r.text else {}


def neo4j_http_url() -> str:
    """Aura Query API base: neo4j+s://host -> https://host."""
    host = urlparse(env("NEO4J_URI").replace("neo4j+s://", "https://").replace("neo4j://", "http://")).netloc
    return f"https://{host}"


_NEO4J_DB: str | None = None


def neo4j_database() -> str:
    """The instance's actual home database name. Bolt sessions use the server
    default implicitly, but the HTTP Query API (used by the functions) needs
    the name in the path — and it is NOT always 'neo4j'. Resolve once via Bolt,
    then prove the Query API accepts it."""
    global _NEO4J_DB
    if _NEO4J_DB:
        return _NEO4J_DB
    import graph

    graph.verify_graph()
    with graph.get_driver().session() as s:
        _NEO4J_DB = s.run("CALL db.info() YIELD name RETURN name").single()["name"]
    graph.close_graph()

    r = requests.post(
        f"{neo4j_http_url()}/db/{_NEO4J_DB}/query/v2",
        json={"statement": "RETURN 1"},
        auth=(env("NEO4J_USERNAME"), env("NEO4J_PASSWORD")),
        timeout=30,
    )
    if r.status_code not in (200, 202):
        raise SystemExit(f"Neo4j Query API rejected db '{_NEO4J_DB}': HTTP {r.status_code}: {r.text[:200]}")
    print(f"  neo4j database resolved: '{_NEO4J_DB}' (Query API verified)")
    return _NEO4J_DB


def ensure_ingest_secret() -> str:
    """Shared secret protecting digest-ingest. Minted once, appended to .env."""
    import os

    existing = os.environ.get("UF_INGEST_SECRET")
    if existing:
        return existing
    secret = pysecrets.token_hex(24)
    with open(REPO_ROOT / ".env", "a", encoding="utf-8") as fh:
        fh.write(f"\n# minted by deploy_backend.py — protects the digest-ingest function\nUF_INGEST_SECRET={secret}\n")
    os.environ["UF_INGEST_SECRET"] = secret
    print("  minted UF_INGEST_SECRET and appended it to .env")
    return secret


def apply_schema() -> None:
    # Always apply: the server diffs declaratively (idempotent), and a
    # table-name check would miss column additions.
    call("POST", f"/v1/{APP}/schema/apply", {"schema": json.loads(SCHEMA_FILE.read_text()), "name": "deploy_backend"})
    print("✓ schema applied (declarative diff, idempotent)")


def apply_owner_rls() -> None:
    """Lock every data table to the owner at the database layer, so the
    auto-generated Data API (`GET /v1/<app>/<table>`) is closed to strangers —
    not just the functions. Without this, ANY signed-up user (signup is open)
    could read the tables directly, bypassing the function guard entirely.

    Model: RLS on, one policy per table admitting only the owner's app-user id
    for end-user (butterbase_user) requests. The platform auto-adds a service
    bypass on enable, so the scraper and this script (platform API key) keep
    full access; the owner's own function calls run as butterbase_user with the
    owner id and pass. Rebuilt each deploy (drop → enable → policy) to stay
    idempotent. env() fails loudly if UF_OWNER_USER_ID is missing."""
    owner_id = env("UF_OWNER_USER_ID")
    expr = f"current_user_id() = '{owner_id}'"
    tables = list(json.loads(SCHEMA_FILE.read_text())["tables"].keys())
    for t in tables:
        # Drop any prior RLS/policies so re-runs don't collide on policy name.
        # (This API rejects an empty JSON body, so pass {}.)
        call("DELETE", f"/v1/{APP}/rls/{t}", {}, ok=(200, 404))
        call("POST", f"/v1/{APP}/rls/enable", {"table_name": t}, ok=(200, 201))
        call("POST", f"/v1/{APP}/rls/policies", {
            "table_name": t,
            "policy_name": f"{t}_owner_only",
            "command": "ALL",
            "role": "user",
            "using_expression": expr,
            "with_check_expression": expr,
        }, ok=(200, 201))
    print(f"✓ owner-only RLS on {len(tables)} table(s) (Data API closed to non-owners)")


# Per-function deployment config. auth 'required' unless stated — digest-ingest
# is called by RocketRide (not a Butterbase principal) and guards itself with
# the X-UF-Secret header instead.
#
# Every auth-required function ALSO gets UF_OWNER_USER_ID: platform signup is
# open, so "authenticated" only proves *some* user. The functions 403 anyone
# whose app-user id isn't the owner's — this instance's data (and its AI
# credits) belong to one person. (ctx.user carries only the id, not the email,
# which is why we key off the id.) env() fails loudly if it is missing. This is
# the function-layer guard; apply_owner_rls() closes the raw Data API too.
def function_specs(ingest_secret: str) -> list[dict]:
    owner = {"UF_OWNER_USER_ID": env("UF_OWNER_USER_ID")}
    common_neo4j = {
        "NEO4J_HTTP_URL": neo4j_http_url(),
        "NEO4J_DATABASE": neo4j_database(),
        "NEO4J_USERNAME": env("NEO4J_USERNAME"),
        "NEO4J_PASSWORD": env("NEO4J_PASSWORD"),
    }
    return [
        {
            "name": "chat",
            "description": "Grounded Q&A over scraped items + Neo4j graph, via the Butterbase AI gateway",
            "envVars": {"BUTTERBASE_API_KEY": env("BUTTERBASE_API_KEY"), **owner, **common_neo4j},
            "trigger": {"type": "http", "config": {"auth": "required"}},
            "timeoutMs": 60000,
        },
        {
            "name": "interactions",
            "description": "Records interactions + curation (item/digest feedback, tombstone deletes incl. Neo4j cleanup)",
            "envVars": {**owner, **common_neo4j},
            "trigger": {"type": "http", "config": {"auth": "required"}},
        },
        {
            "name": "digest-latest",
            "description": "Hot digests + recent activity snapshot for the UI",
            "envVars": owner,
            "trigger": {"type": "http", "config": {"auth": "required"}},
        },
        {
            "name": "graph-data",
            "description": "Social graph (contacts/topics/mention edges) shaped for NVL visualization",
            "envVars": {**owner, **common_neo4j},
            "trigger": {"type": "http", "config": {"auth": "required"}},
        },
        {
            "name": "digest-ingest",
            "description": "Ingests digests from the RocketRide pipeline (X-UF-Secret protected)",
            "envVars": {"UF_INGEST_SECRET": ingest_secret},
            "trigger": {"type": "http", "config": {"auth": "none"}},
        },
    ]


def deploy_functions(ingest_secret: str) -> None:
    for spec in function_specs(ingest_secret):
        code = (FUNCTIONS_DIR / f"{spec['name']}.ts").read_text()
        body = {**spec, "code": code}
        call("POST", f"/v1/{APP}/functions", body)
        print(f"✓ function {spec['name']} deployed")


def set_cors() -> None:
    call("PATCH", f"/v1/{APP}/config/cors",
         {"allowed_origins": [FRONTEND_URL, "http://localhost:8787"]}, ok=(200,))
    print(f"✓ CORS: {FRONTEND_URL} (+ localhost:8787 for dev)")


def smoke(ingest_secret: str) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    r = requests.post(
        f"{API}/v1/{APP}/fn/digest-ingest",
        headers={"Content-Type": "application/json", "X-UF-Secret": ingest_secret},
        json={
            "headline": "Unlimited Focus backend is live",
            "body": "Deployed: chat, interactions, digest-latest, digest-ingest. "
                    "Run the scraper, then digest_run.py, and this panel fills with real trends.",
            "source": "deploy-smoke",
            "period_start": now.isoformat(),
            "period_end": now.isoformat(),
        },
        timeout=60,
    )
    if r.status_code == 200:
        print(f"✓ smoke: digest-ingest accepted (digest id {r.json().get('id')})")
    else:
        raise SystemExit(f"✗ smoke: digest-ingest -> HTTP {r.status_code}: {r.text[:300]}")


def billing_setup() -> None:
    """Stripe Connect onboarding + the Focus Pro plan (idempotent)."""
    status = call("GET", f"/v1/{APP}/billing/connect/status", ok=(200, 404))
    print(f"  connect status: {json.dumps(status)[:200]}")
    if not (status.get("payoutsEnabled") or status.get("payouts_enabled")):
        ob = call("POST", f"/v1/{APP}/billing/connect/onboard", {})
        print("  → ACTION NEEDED: finish Stripe onboarding here:\n    " + str(ob.get("onboardingUrl")))
    plans = call("GET", f"/v1/{APP}/billing/plans")
    plan_list = plans if isinstance(plans, list) else plans.get("plans", [])
    if not plan_list:
        plan = call("POST", f"/v1/{APP}/billing/plans", {
            "name": "Focus Pro",
            "priceCents": 500,
            "interval": "month",
            "features": [
                "Overnight scraping across platforms",
                "Daily AI digests of your circles",
                "Knowledge-graph friend timeline",
                "Long-term memory of friends' milestones",
            ],
        })
        print(f"✓ plan created: Focus Pro $5/mo ({plan.get('id')})")
    else:
        print(f"✓ plan exists: {plan_list[0].get('name')} ({plan_list[0].get('id')})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--billing", action="store_true", help="also set up Stripe Connect + the Focus Pro plan")
    args = ap.parse_args()

    print(f"deploying backend to {APP} …")
    ingest_secret = ensure_ingest_secret()
    apply_schema()
    apply_owner_rls()
    deploy_functions(ingest_secret)
    set_cors()
    smoke(ingest_secret)
    if args.billing:
        billing_setup()
    print("\nbackend deployed. Frontend calls:")
    print(f"  POST {API}/v1/{APP}/fn/chat | /fn/interactions | GET /fn/digest-latest")


if __name__ == "__main__":
    main()
