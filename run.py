#!/usr/bin/env python3
"""WariVerse — one file, whole system.

    python run.py

That is it. From a clean clone to a working stack with demo data loaded:
config generated, containers built, migrations applied, seed data in, and every
URL and login printed at the end.

    python run.py                    start everything (default)
    python run.py --observability    also start Prometheus + Grafana
    python run.py --stop             stop, keep the data
    python run.py --reset            DESTROY the data and start clean
    python run.py --logs [service]   follow logs
    python run.py --status           what is running, and is it healthy
    python run.py --no-seed          start without demo data
    python run.py --gemini-key KEY   optional free AI Studio key

NO EXTERNAL ACCOUNTS. NO PAID SERVICES. NO API KEYS REQUIRED.
Crowd analytics run on the built-in simulation engine, the map draws our own
polygons with no tile server, and OTP codes come back in the API response in
development. The only external key the project can use is a free-tier Gemini
key, and the assistant works without it.

---

Why this file is defensive rather than a three-line shell script: every check
below exists because the failure it catches produces a confusing symptom rather
than a clear error. A hung Docker daemon looks like a slow build. A full disk
looks like Postgres hanging. A .env written by a shell redirect on Windows looks
like a config parse error pointing at the wrong line. Each of those costs
twenty minutes to diagnose and five seconds to detect.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
GEN_ENV = ROOT / "services" / "core-api" / "scripts" / "gen_env.py"

#: Services that must be healthy before the stack is usable. `scheduler` and the
#: front ends are deliberately absent: the scheduler serves no HTTP and has its
#: healthcheck disabled, and a front end that is still building should not hold
#: up an API that already works.
CORE_SERVICES = ["db", "redis", "core-api"]
ALL_SERVICES = CORE_SERVICES + ["scheduler", "ai-engine", "admin-console", "pilgrim-pwa"]

API = "http://localhost:8000"

#: A first build pulls Postgres/Timescale, Redis, two Node images and installs
#: two Python dependency sets. On a slow connection that is genuinely minutes.
BUILD_TIMEOUT = 1800
HEALTH_TIMEOUT = 300

#: Docker needs headroom for images, volumes and the WAL. Below this a build
#: fails in ways that look like network errors, and Postgres hangs rather than
#: reporting a disk problem.
MIN_FREE_GB = 5


# ---------------------------------------------------------------------------
# output — ASCII only
# ---------------------------------------------------------------------------
# A Windows console is cp1252. Box-drawing characters and em-dashes render as
# mojibake there, and the first thing a new user sees should not look broken.
def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: int, total: int, msg: str) -> None:
    say(f"\n[{n}/{total}] {msg}")


def ok(msg: str) -> None:
    say(f"  OK    {msg}")


def warn(msg: str) -> None:
    say(f"  WARN  {msg}")


def fail(msg: str, *, hint: str = "") -> None:
    say(f"\n  FAIL  {msg}")
    if hint:
        for line in hint.strip().splitlines():
            say(f"        {line}")
    say()


def rule(title: str = "") -> None:
    say("=" * 68)
    if title:
        say(f"  {title}")
        say("=" * 68)


# ---------------------------------------------------------------------------
# shell helpers
# ---------------------------------------------------------------------------
def run(cmd: list[str], *, timeout: int | None = None, capture: bool = True,
        check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=ROOT,
        timeout=timeout,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def compose(*args: str) -> list[str]:
    return ["docker", "compose", *args]


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------
def check_docker() -> bool:
    """Docker installed, and the daemon actually answering.

    These are two different failures. `docker` on PATH with a dead daemon is the
    nastier one: every later command hangs instead of erroring, so the whole
    thing looks like a very slow build. A short timeout on `docker info` turns
    twenty minutes of confusion into one line.

    **This function is the gate for every other Docker call in this file, and
    that is deliberate.** On Windows, `subprocess.run(timeout=...)` is only
    dependable against `docker` itself. `docker compose` is a CLI plugin that
    spawns a *child* process, so when the daemon is wedged the timeout fires,
    Python kills the parent, and then blocks in `wait()` on the surviving child
    — the timeout silently fails to bound anything. `docker info` has no such
    child and does time out cleanly. So: prove the daemon is alive with a bare
    `docker info` first, and only then run compose commands.
    """
    if shutil.which("docker") is None:
        fail(
            "Docker is not installed, or not on PATH.",
            hint="Install Docker Desktop: https://docs.docker.com/get-docker/\n"
                 "Then run this script again.",
        )
        return False

    try:
        result = run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=25)
    except subprocess.TimeoutExpired:
        fail(
            "Docker is installed but the daemon is not responding.",
            hint="It is probably still starting, or wedged.\n"
                 "  1. Open Docker Desktop and wait for 'Engine running'\n"
                 "  2. If it never gets there: quit it fully, then reopen\n"
                 "  3. On Windows a stubborn daemon usually needs:  wsl --shutdown",
        )
        return False

    if result.returncode != 0:
        fail(
            "Docker daemon is not reachable.",
            hint="Start Docker Desktop (or `sudo systemctl start docker`) and retry.",
        )
        return False

    ok(f"Docker daemon {result.stdout.strip() or 'running'}")
    return True


def check_disk() -> bool:
    """Refuse to start with no room.

    Learned the hard way: a full disk does not present as 'disk full'. Postgres
    accepts TCP connections and then hangs on the first query, image builds fail
    with what look like network errors, and nothing says why.
    """
    free_gb = shutil.disk_usage(ROOT).free / (1024 ** 3)
    if free_gb < MIN_FREE_GB:
        fail(
            f"Only {free_gb:.1f} GB free. Need at least {MIN_FREE_GB} GB.",
            hint="Docker images, the database volume and the WAL all need room.\n"
                 "Reclaim regenerable space safely with:\n"
                 "    docker builder prune -f      (build cache, no data touched)\n"
                 "    docker image prune -f        (dangling images)",
        )
        return False
    ok(f"{free_gb:.1f} GB free")
    return True


def ensure_env(gemini_key: str | None) -> bool:
    """Generate .env if absent.

    Delegates to gen_env.py rather than duplicating it, so there is one place
    that knows a valid Fernet key is required for CONTACT_ENCRYPTION_KEY. That
    one is not an arbitrary string: a placeholder decodes to the wrong length
    and Fernet rejects it, invisibly, until the first thing that encrypts.
    """
    if ENV_FILE.exists():
        ok(".env exists (delete it to regenerate)")
        return True

    if not GEN_ENV.exists():
        fail(f"Missing {GEN_ENV.relative_to(ROOT)}", hint="Is this a complete clone?")
        return False

    cmd = [sys.executable, str(GEN_ENV)]
    if gemini_key:
        cmd += ["--gemini-key", gemini_key]

    result = run(cmd, timeout=60)
    if result.returncode != 0 or not ENV_FILE.exists():
        fail("Could not generate .env", hint=(result.stderr or result.stdout).strip())
        return False

    ok("Generated .env with fresh local secrets (no external account used)")
    if not gemini_key:
        say("        Assistant will run in its deterministic mode.")
        say("        Optional free key: https://aistudio.google.com/apikey")
    return True


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
def start_stack(profiles: list[str]) -> bool:
    args = []
    for profile in profiles:
        args += ["--profile", profile]

    say("        Building images. First run pulls ~2 GB and takes a few minutes.")
    say("        Later runs are seconds. Progress below:\n")

    # Not captured: the user needs to see build progress on a first run, or a
    # five-minute silence looks like a hang.
    try:
        result = subprocess.run(
            compose(*args, "up", "-d", "--build"),
            cwd=ROOT,
            timeout=BUILD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        fail(
            f"Build exceeded {BUILD_TIMEOUT // 60} minutes.",
            hint="Usually a slow or blocked image pull.\n"
                 "Check connectivity, then:  python run.py --logs",
        )
        return False

    if result.returncode != 0:
        fail(
            "docker compose up failed.",
            hint="Read the output above. Common causes:\n"
                 "  - a port already in use (8000, 5432, 6379, 5173, 5174)\n"
                 "  - not enough disk space\n"
                 "  - a stale container:  python run.py --reset",
        )
        return False

    ok("Containers started")
    return True


def wait_for_health() -> bool:
    """Poll until the API answers, or explain why it never will.

    Container-running and application-ready are different states. core-api runs
    `alembic upgrade head` before uvicorn, so on a first run it is up but not
    listening for a while — polling the HTTP endpoint is the only honest check.
    """
    say("        Waiting for migrations and startup...")
    deadline = time.monotonic() + HEALTH_TIMEOUT
    last_note = ""

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{API}/health", timeout=4) as response:  # noqa: S310
                if response.status == 200:
                    ok("API is serving")
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass

        # If a container has already died, stop waiting for it.
        dead = crashed_services()
        if dead:
            fail(
                f"Container exited: {', '.join(dead)}",
                hint=f"Look at why:  python run.py --logs {dead[0]}",
            )
            return False

        elapsed = int(HEALTH_TIMEOUT - (deadline - time.monotonic()))
        note = f"        ... {elapsed}s"
        if note != last_note:
            print(note, end="\r", flush=True)
            last_note = note
        time.sleep(3)

    fail(
        f"API did not become ready within {HEALTH_TIMEOUT}s.",
        hint="Most often migrations failed against the database.\n"
             "    python run.py --logs core-api",
    )
    return False


def crashed_services() -> list[str]:
    """Services whose container has exited.

    Returns empty on ANY inspection failure, including a timeout. Two reasons,
    and the second is the one that bit during testing:

    * A failure to inspect is not evidence that something died. Reporting a
      crash we did not observe would send someone reading logs for a container
      that is fine.
    * This runs inside the `wait_for_health` poll loop. If the daemon wedges
      mid-startup, an uncaught `TimeoutExpired` would abort the wait with a
      traceback instead of letting the loop reach its own deadline and print
      the actionable message.
    """
    try:
        result = run(compose("ps", "--format", "json"), timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []

    dead: list[str] = []
    for line in result.stdout.strip().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("State", "")).lower() in {"exited", "dead"}:
            name = row.get("Service") or row.get("Name") or "?"
            # A one-shot container that exited 0 did its job.
            if str(row.get("ExitCode", 0)) not in {"0"}:
                dead.append(name)
    return dead


def seed() -> bool:
    say("        Loading demo data (zones, cameras, staff, Wari route)...")
    result = run(
        compose("exec", "-T", "core-api", "python", "scripts/seed_dev.py"),
        timeout=300,
    )
    # The seed is idempotent; a re-run finding everything present is success.
    if result.returncode != 0 and "complete" not in (result.stdout or ""):
        warn("Seeding reported a problem - the stack is still usable.")
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        for line in tail:
            say(f"        {line}")
        return False
    ok("Demo data loaded")
    return True


def summary(profiles: list[str]) -> None:
    say()
    rule("WariVerse is running")
    say("""
  OPEN THESE
    Admin console (control room)   http://localhost:5173
    Pilgrim app (Marathi, mobile)  http://localhost:5174
    API docs (try any endpoint)    http://localhost:8000/docs
    Health                         http://localhost:8000/health/deep
""")
    if "observability" in profiles:
        say("    Grafana (crowd pipeline)       http://localhost:3000  (admin/admin)")
        say("    Prometheus                     http://localhost:9090\n")

    say("""  SIGN IN
    Staff, password  wari-demo-2026-change-me
      9000000001  system_admin        9000000004  volunteer
      9000000002  administrator       9000000005  responder
      9000000003  security_officer

    Administrator and System Admin need a TOTP code. In development the seed
    enrols them with DEV_MFA_SECRET, so any authenticator app produces a
    working one. Or use the one-click dev sign-in buttons on the console.

    Pilgrims sign in by phone OTP, and the code comes back in the API response
    (OTP_DEBUG_ECHO=true) - which is why no SMS account is needed.

  WHAT IS RUNNING
    Crowd data is the built-in SIMULATION engine (CROWD_SOURCE=sim). No
    cameras, no GPU, no cloud vision API. Zones fill with realistic crowd
    that rises and falls; the stagnation index moves before density does,
    which is the alert this system exists to fire.

  USEFUL
    python run.py --logs           follow everything
    python run.py --logs core-api  follow one service
    python run.py --status         health at a glance
    python run.py --stop           stop, keep data
    python run.py --reset          destroy data, start clean
""")
    rule()


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------
def do_stop() -> int:
    if not check_docker():
        return 1
    say("Stopping (data is kept)...")
    result = subprocess.run(compose("--profile", "observability", "stop"), cwd=ROOT)
    if result.returncode == 0:
        ok("Stopped. `python run.py` brings it back with your data intact.")
    return result.returncode


def do_reset() -> int:
    # Checked before the confirmation prompt, not after: asking someone to type
    # "yes" to destroy their data and then hanging on a dead daemon is the worst
    # ordering available.
    if not check_docker():
        return 1

    rule("RESET - this DESTROYS the database")
    say("\n  Every pass, incident, breach record and seeded zone will be deleted.")
    say("  Your .env is kept, so secrets and any Gemini key survive.\n")
    try:
        if input("  Type 'yes' to confirm: ").strip().lower() != "yes":
            say("\n  Cancelled. Nothing was changed.")
            return 1
    except (EOFError, KeyboardInterrupt):
        say("\n  Cancelled.")
        return 1

    say("\nRemoving containers and volumes...")
    subprocess.run(compose("--profile", "observability", "down", "-v"), cwd=ROOT)
    ok("Clean. Run `python run.py` to rebuild from scratch.")
    return 0


def do_logs(service: str | None) -> int:
    if not check_docker():
        return 1
    target = [service] if service else []
    say(f"Following logs{' for ' + service if service else ''}. Ctrl-C to stop.\n")
    try:
        return subprocess.run(compose("logs", "-f", "--tail", "60", *target), cwd=ROOT).returncode
    except KeyboardInterrupt:
        return 0


def do_status() -> int:
    # Gated on `check_docker` for the reason documented there: a compose call
    # against a wedged daemon cannot be reliably timed out on Windows. And
    # `--status` is precisely what someone runs when things are behaving oddly,
    # so it is the last command that should itself hang.
    if not check_docker():
        return 1

    rule("Status")
    subprocess.run(compose("ps"), cwd=ROOT)
    say()
    try:
        with urllib.request.urlopen(f"{API}/health/deep", timeout=6) as response:  # noqa: S310
            body = json.loads(response.read())
        say("  /health/deep:")
        say("  " + json.dumps(body, indent=2).replace("\n", "\n  "))
    except Exception:
        warn("API is not answering on :8000. Try: python run.py --logs core-api")
    return 0


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the whole WariVerse stack.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="No external accounts. No paid services. No API keys required.",
    )
    parser.add_argument("--stop", action="store_true", help="stop, keeping data")
    parser.add_argument("--reset", action="store_true", help="destroy data and start clean")
    parser.add_argument("--status", action="store_true", help="show health")
    parser.add_argument("--logs", nargs="?", const="", metavar="SERVICE", help="follow logs")
    parser.add_argument("--no-seed", action="store_true", help="skip demo data")
    parser.add_argument("--observability", action="store_true",
                        help="also start Prometheus and Grafana")
    parser.add_argument("--gemini-key", metavar="KEY",
                        help="optional free AI Studio key for the assistant")
    args = parser.parse_args()

    if args.stop:
        return do_stop()
    if args.reset:
        return do_reset()
    if args.status:
        return do_status()
    if args.logs is not None:
        return do_logs(args.logs or None)

    profiles = ["observability"] if args.observability else []
    total = 5 if not args.no_seed else 4

    rule("WariVerse")
    say("\n  Crowd safety and pilgrim management for the Pandharpur Wari.")
    say("  No external accounts, no paid services, no API keys required.")

    step(1, total, "Checking prerequisites")
    if not check_docker() or not check_disk():
        return 1

    step(2, total, "Configuration")
    if not ensure_env(args.gemini_key):
        return 1

    step(3, total, "Starting services")
    if not start_stack(profiles):
        return 1

    step(4, total, "Waiting for the API")
    if not wait_for_health():
        return 1

    if not args.no_seed:
        step(5, total, "Demo data")
        seed()

    summary(profiles)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say("\n\nInterrupted. Containers may still be running:")
        say("  python run.py --status     python run.py --stop")
        raise SystemExit(130) from None
