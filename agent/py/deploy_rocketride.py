#!/usr/bin/env python3
"""Deploy the digest pipeline to RocketRide Cloud as a managed deployment.

Registers agent/pipeline/digest_pipeline.json with a daily schedule, so it
lives on RocketRide Cloud (hackathon mandate: managed, production — not just
local). digest_run.py executes runs through the same cloud runtime with fresh
context per run.

Usage:  uv run deploy_rocketride.py [--schedule "@daily" | manual]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from uf_env import AGENT_ROOT, env

PIPELINE_FILE = AGENT_ROOT / "pipeline" / "digest_pipeline.json"


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--schedule", default="@daily")
    args = ap.parse_args()

    from rocketride import RocketRideClient

    # Present for parity with digest_run; the deploy stores the pipeline with
    # ${ROCKETRIDE_*} UNRESOLVED (secrets aren't uploaded), so scheduled cloud
    # runs need this key set in the RocketRide Cloud project env too. The
    # app-driven path (digest_run.py) resolves it client-side and always works.
    os.environ["ROCKETRIDE_BB_KEY"] = env("BUTTERBASE_API_KEY")

    uri = os.environ.get("ROCKETRIDE_URI", "https://api.rocketride.ai")
    pipeline = json.loads(PIPELINE_FILE.read_text())

    project_id = pipeline["project_id"]
    async with RocketRideClient(uri=uri, auth=env("ROCKET_RIDE_API_KEY")) as client:
        try:
            await client.deploy.add(pipeline, schedule=args.schedule)
            print(f"✓ deployed '{project_id}' to RocketRide Cloud (schedule={args.schedule})")
        except RuntimeError as exc:
            if "already exists" not in str(exc):
                raise
            await client.deploy.update(project_id, pipeline=pipeline, schedule=args.schedule)
            print(f"✓ updated existing deployment '{project_id}' (schedule={args.schedule})")

        print("\ncurrent deployments:")
        for rec in await client.deploy.list():
            p = rec.get("pipeline", {}) if isinstance(rec, dict) else {}
            print(f"  {p.get('project_id') or rec}  schedule={rec.get('schedule')}  state={rec.get('state')}")


if __name__ == "__main__":
    asyncio.run(main())
