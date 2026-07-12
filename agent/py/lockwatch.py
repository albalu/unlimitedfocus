#!/usr/bin/env python3
"""Scrape while you're away: run both scrapers whenever the Mac screen locks.

A tiny always-on watcher (installed as a launchd LaunchAgent) listens for
macOS's `com.apple.screenIsLocked` distributed notification. When the screen
locks and the last triggered run started >= UF_LOCKWATCH_GAP_MIN minutes ago
(default 10), it runs scrape_instagram.py then scrape_linkedin.py —
SEQUENTIALLY, never in parallel: both drive the last tab of Chrome window 1,
so concurrent runs would fight over the same tab.

Coming back is handled too: on `com.apple.screenIsUnlocked` any in-flight
run is interrupted with SIGINT — the scrapers' finally blocks then restore
the Unlimited Focus extension, close their tab, and mark the run failed;
whatever was captured stays in the daily cache and commits on the next run.
Your Chrome is yours again the moment you sit down.

Runs are wrapped in `caffeinate -i` so the Mac doesn't idle-sleep mid-scrape
while locked (a closed lid still sleeps — no runs happen then; the cache
makes any interrupted run safe to resume).

Usage:
    uv run lockwatch.py install      # write + load the LaunchAgent (idempotent)
    uv run lockwatch.py uninstall    # unload + remove it
    uv run lockwatch.py status       # is it loaded? when did it last run?
    uv run lockwatch.py fire         # post a fake lock event to the running
                                     # watcher — use once after install, while
                                     # you're at the machine, so macOS shows
                                     # its automation prompts where you can
                                     # actually approve them
    uv run lockwatch.py watch        # the daemon loop (launchd runs this)

First-run permissions: launchd gives the watcher its own automation identity,
so the first triggered run makes macOS ask again for "control Google Chrome"
— that's what `fire` is for: trigger it while unlocked and click Allow once.

Trust note: any local process could post the same notification and start a
scrape early. That's the same deliberately-weak, low-stakes trust model as
the extension's agent bridge — the worst an abuser achieves is a polite,
rate-limited scrape you'd have run anyway.
"""
from __future__ import annotations

import datetime as dt
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import threading
import time

from uf_env import DATA_DIR

AGENT_PY = os.path.dirname(os.path.abspath(__file__))
LABEL = "com.unlimitedfocus.lockwatch"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
STAMP = DATA_DIR / "lockwatch_last_run"
LOG = DATA_DIR / "lockwatch.log"
LAUNCHD_LOG = DATA_DIR / "lockwatch-launchd.log"

GAP_MIN = int(os.environ.get("UF_LOCKWATCH_GAP_MIN") or 10)
# Comma-separated override for testing / future platforms.
SCRAPERS = [s.strip() for s in
            (os.environ.get("UF_LOCKWATCH_SCRAPERS") or "scrape_instagram.py,scrape_linkedin.py")
            .split(",") if s.strip()]

LOCKED_NOTE = "com.apple.screenIsLocked"
UNLOCKED_NOTE = "com.apple.screenIsUnlocked"


def log(*args) -> None:
    line = " ".join([dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *map(str, args)])
    print(line, flush=True)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ── the debounced runner ──────────────────────────────────────────────────────

class Runner:
    """Runs the scrapers sequentially in their own process group; at most one
    sequence at a time; SIGINT-able from the unlock handler (SIGINT, not
    SIGTERM, so the scrapers' finally blocks run: extension restored, tab
    closed, run row marked failed)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.current: subprocess.Popen | None = None
        self.running = False
        self.abort = False

    def last_run_age_min(self) -> float | None:
        try:
            return (time.time() - float(STAMP.read_text().strip())) / 60
        except (OSError, ValueError):
            return None

    def trigger(self, reason: str, force: bool = False) -> None:
        with self.lock:
            if self.running:
                log(f"lock event ({reason}) — a run is already in progress, skipping")
                return
            age = self.last_run_age_min()
            if not force and age is not None and age < GAP_MIN:
                log(f"lock event ({reason}) — last run {age:.1f} min ago (< {GAP_MIN}), skipping")
                return
            self.running = True
            self.abort = False
        threading.Thread(target=self._run_all, args=(reason,), daemon=True).start()

    def interrupt(self, reason: str) -> None:
        with self.lock:
            if not self.running:
                return
            self.abort = True
            proc = self.current
        if proc and proc.poll() is None:
            log(f"{reason} — interrupting the in-flight scraper (SIGINT, graceful)")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            except (ProcessLookupError, PermissionError):
                pass

    def _run_all(self, reason: str) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            STAMP.write_text(str(time.time()))
            log(f"▶ triggered by {reason} — running {', '.join(SCRAPERS)}")
            for script in SCRAPERS:
                if self.abort:
                    log(f"  ⏹ aborted before {script} (screen unlocked)")
                    break
                # caffeinate -i: no idle sleep while a scraper is going
                cmd = ["caffeinate", "-i", "uv", "run", script]
                with open(LOG, "a", encoding="utf-8") as sink:
                    with self.lock:
                        self.current = subprocess.Popen(
                            cmd, cwd=AGENT_PY, start_new_session=True,
                            stdout=sink, stderr=subprocess.STDOUT,
                        )
                    rc = self.current.wait()
                log(f"  {script} exited {rc}" + (" (interrupted)" if self.abort else ""))
            log("■ sequence done")
        except Exception as exc:
            log(f"✗ runner error: {exc!r}")
        finally:
            with self.lock:
                self.running = False
                self.current = None


# ── the watcher daemon (launchd runs `watch`) ─────────────────────────────────

def watch() -> None:
    from Foundation import NSDistributedNotificationCenter, NSObject, NSRunLoop  # noqa: PLC0415

    runner = Runner()
    log(f"watcher up — gap {GAP_MIN} min, scrapers: {', '.join(SCRAPERS)}")

    class Observer(NSObject):
        def locked_(self, note):  # noqa: N802 (ObjC selector naming)
            forced = bool((note.userInfo() or {}).get("force"))
            runner.trigger("forced fire" if forced else "screen lock", force=forced)

        def unlocked_(self, note):  # noqa: N802
            runner.interrupt("screen unlocked")

    observer = Observer.alloc().init()
    center = NSDistributedNotificationCenter.defaultCenter()
    center.addObserver_selector_name_object_(observer, b"locked:", LOCKED_NOTE, None)
    center.addObserver_selector_name_object_(observer, b"unlocked:", UNLOCKED_NOTE, None)
    NSRunLoop.currentRunLoop().run()  # forever; launchd owns the lifecycle


# ── install / uninstall / status / fire ───────────────────────────────────────

def _uid() -> int:
    return os.getuid()


def install() -> None:
    uv = shutil.which("uv")
    claude = shutil.which("claude")
    if not uv:
        raise SystemExit("uv not found on PATH — install it first")
    if not claude:
        raise SystemExit("claude CLI not found on PATH — the scrapers need it for extraction")
    # launchd starts agents with a bare PATH; bake in everything the scrapers
    # resolve at runtime (uv, claude, caffeinate, osascript are absolute/std).
    path = ":".join(dict.fromkeys(  # ordered de-dupe
        [os.path.dirname(uv), os.path.dirname(claude),
         "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    ))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [uv, "run", "lockwatch.py", "watch"],
        "WorkingDirectory": AGENT_PY,
        "EnvironmentVariables": {"PATH": path},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LAUNCHD_LOG),
        "StandardErrorPath": str(LAUNCHD_LOG),
    }
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "wb") as fh:
        plistlib.dump(plist, fh)
    subprocess.run(["launchctl", "bootout", f"gui/{_uid()}/{LABEL}"],
                   capture_output=True)  # reload cleanly if already installed
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{_uid()}", PLIST],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}")
    print(f"""✓ installed and running: {LABEL}
  plist   {PLIST}
  log     {LOG}
  gap     {GAP_MIN} min between triggered runs (UF_LOCKWATCH_GAP_MIN in .env to change)

Next (one time, do it now while you're at the machine):
  uv run lockwatch.py fire
macOS will ask to let the watcher control Google Chrome / send Apple events —
click Allow. Without this, the first real lock-triggered run fails silently
behind the lock screen. Then watch it work: tail -f {LOG}""")


def uninstall() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{_uid()}/{LABEL}"], capture_output=True)
    try:
        os.remove(PLIST)
    except FileNotFoundError:
        pass
    print(f"✓ {LABEL} unloaded and plist removed")


def status() -> None:
    loaded = subprocess.run(["launchctl", "print", f"gui/{_uid()}/{LABEL}"],
                            capture_output=True).returncode == 0
    print(f"watcher: {'loaded ✓' if loaded else 'NOT loaded ✗ (uv run lockwatch.py install)'}")
    try:
        ts = float(STAMP.read_text().strip())
        print(f"last triggered run: {dt.datetime.fromtimestamp(ts):%Y-%m-%d %H:%M:%S} "
              f"({(time.time() - ts) / 60:.0f} min ago)")
    except (OSError, ValueError):
        print("last triggered run: never")
    if LOG.exists():
        tail = LOG.read_text(encoding="utf-8").splitlines()[-8:]
        print("recent log:\n  " + "\n  ".join(tail))


def fire() -> None:
    """Post the same distributed notification a real screen lock posts, plus
    force=1 so the running watcher ignores the 10-minute gap. Runs in the
    WATCHER's process (the launchd context), which is exactly where macOS
    needs to see the automation approval happen."""
    from Foundation import NSDistributedNotificationCenter  # noqa: PLC0415

    NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
        LOCKED_NOTE, None, {"force": 1}, True
    )
    print(f"fake lock event posted (gap check bypassed) — watch it: tail -f {LOG}")


def main() -> None:
    cmds = {"install": install, "uninstall": uninstall, "status": status,
            "fire": fire, "watch": watch}
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in cmds:
        raise SystemExit(f"usage: uv run lockwatch.py {{{'|'.join(cmds)}}}\n\n{__doc__}")
    cmds[cmd]()


if __name__ == "__main__":
    main()
