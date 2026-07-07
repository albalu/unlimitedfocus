"""Text + image understanding via the local `claude` CLI so extraction runs on
the cheap Sonnet tier through the Claude Code install, not a metered API key.
TODO(cost): batch several items per invocation once the schema settles."""
from __future__ import annotations

import json
import os
import subprocess

from uf_env import AGENT_ROOT

MODEL = "sonnet"  # alias -> latest Sonnet


def clean_env() -> dict:
    """Env for the claude subprocess, with auth-hijacking variables removed.

    The scraper process carries repo .env contents (dotenv) and may run inside
    a Claude Code session; an inherited ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL /
    CLAUDE_CODE_* would make `claude -p` authenticate differently than it does
    in the user's plain terminal (symptom: `API Error: 401` on stdout).
    Stripping them makes the CLI fall back to its own stored login.
    """
    env = os.environ.copy()
    for k in list(env):
        if k.startswith("CLAUDE_CODE_") or k in (
            "CLAUDECODE", "CLAUDE_EFFORT",
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        ):
            env.pop(k)
    return env


def run_claude(prompt: str, timeout: int = 180) -> str:
    """Run `claude -p` headless; return stdout. Raises with the FULL error text
    (claude prints failures like 401s to stdout, not stderr)."""
    result = subprocess.run(
        ["claude", "-p", prompt, "--model", MODEL, "--allowedTools", "Read", "--max-turns", "4"],
        capture_output=True, text=True, timeout=timeout,
        cwd=str(AGENT_ROOT), env=clean_env(),
    )
    if result.returncode != 0:
        detail = " ".join(x for x in [result.stdout.strip(), result.stderr.strip()] if x)
        raise RuntimeError(f"claude CLI failed (exit {result.returncode}): {detail[:400]}")
    return result.stdout


def extract_item(kind: str, username: str, image_path: str | None, raw_text: str | None) -> dict:
    parts = ["You are a strict extraction engine for a personal social-media digest."]
    if image_path:
        parts.append(f"Read the image at {image_path}")
    if raw_text:
        parts.append(f"Raw text scraped alongside it (caption, counts, comment previews):\n---\n{raw_text[:3000]}\n---")
    if not image_path and not raw_text:
        raise ValueError("nothing to extract from (no image, no text)")
    parts.append(
        f"It is an Instagram {kind} by @{username}.\n"
        'Respond with ONLY minified JSON (no markdown fences, no prose) with exactly these keys:\n'
        '{"media_type":"image|video|carousel|text|unknown",'
        '"topic":"<1-3 word topic>",'
        '"ocr_text":"<verbatim text visible in the media, empty string if none>",'
        '"brief":"<1-2 sentence summary>",'
        '"detail":"<3-6 sentence detailed description: what is happening, who, where, when if visible>",'
        '"noteworthy":["<life events worth remembering long-term: birthdays, weddings, moves, travel, launches, achievements — empty array if none>"],'
        '"mentions":["<other instagram usernames referenced, without the @>"]}'
    )
    return _parse_json_loose(run_claude("\n".join(parts)))


def _parse_json_loose(out: str) -> dict:
    """The CLI occasionally wraps output in fences or adds a stray sentence —
    pull the outermost JSON object. Anything less parseable is a hard error: the
    item is skipped this run and retried next run (dedupe only skips items that
    made it into the DB)."""
    text = out.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise RuntimeError(f"extractor returned no JSON: {text[:200]}")
    return json.loads(text[start:end + 1])
