"""Generate a ready-to-run `.env` with real secrets.

    python services/core-api/scripts/gen_env.py
    docker compose up -d

That is the whole setup. **No account anywhere, no key from anyone, nothing
paid.** Every secret below is generated locally on your machine by this script.

Why this exists rather than "copy .env.example and edit it": three of the values
the system needs are not arbitrary strings.

* `CONTACT_ENCRYPTION_KEY` must be a valid **Fernet** key — 32 raw bytes,
  url-safe base64. A plausible-looking placeholder decodes to the wrong length
  and Fernet rejects it, which stays invisible until something actually
  encrypts. In practice that is the first time you register a Dindi or raise an
  SOS with a callback number, which during a demo is the worst possible moment
  to discover it. `.env.example` ships it blank for exactly that reason, and
  blank raises a clean 503 rather than a confusing crash — but blank still means
  those two flows do not work.
* `JWT_SECRET`, `PHONE_HASH_SECRET`, `QR_SIGNING_SECRET` and `AI_SERVICE_TOKEN`
  all ship as `dev-only-...` strings that `assert_production_safe()` refuses to
  boot with in production. Generating them now means the same file works in both
  places.

Two of those have a property worth understanding before you regenerate them
casually: **`PHONE_HASH_SECRET` orphans every stored phone hash** if it changes,
and **`QR_SIGNING_SECRET` invalidates every pass already in a pilgrim's pocket.**
Treat both as permanent for the duration of an event.

The script **writes the file itself** rather than printing for you to redirect.
That is not a style preference: on Windows, `python gen_env.py > .env` encodes
stdout as cp1252, and `Settings` reads `.env` as UTF-8 — so a redirect produces
a file the application then refuses to parse, with an error that points nowhere
near the cause. Writing with an explicit encoding removes the trap.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from datetime import date
from pathlib import Path

try:
    from cryptography.fernet import Fernet
except ImportError:  # pragma: no cover - dependency is in requirements.txt
    print(
        "cryptography is not installed. Run:\n"
        "    pip install -r services/core-api/requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1) from None


def token(length: int = 48) -> str:
    return secrets.token_urlsafe(length)


TEMPLATE = """\
# ---------------------------------------------------------------------------
# WariVerse — generated {today} by scripts/gen_env.py
#
# EXTERNAL SERVICES REQUIRED: none.  PAID SERVICES REQUIRED: none.
# Every secret here was generated locally. Nothing was fetched from a vendor.
#
# Do not commit this file.
# ---------------------------------------------------------------------------

# --- Postgres (TimescaleDB + PostGIS), self-hosted via docker compose -------
POSTGRES_USER=wariverse
POSTGRES_PASSWORD={db_password}
POSTGRES_DB=wariverse
POSTGRES_PORT=5432
DATABASE_URL=postgresql+psycopg://wariverse:{db_password}@db:5432/wariverse

# --- Redis, self-hosted ------------------------------------------------------
REDIS_URL=redis://redis:6379/0

# --- Core API ---------------------------------------------------------------
CORE_API_PORT=8000
ENVIRONMENT=development
LOG_LEVEL=INFO

# Generated locally. Rotating PHONE_HASH_SECRET orphans every stored phone
# hash; rotating QR_SIGNING_SECRET invalidates every pass already issued.
JWT_SECRET={jwt}
PHONE_HASH_SECRET={phone_hash}
CONTACT_ENCRYPTION_KEY={fernet}
QR_SIGNING_SECRET={qr}

ACCESS_TOKEN_TTL_MINUTES=15
REFRESH_TOKEN_TTL_DAYS=7

CORS_ORIGINS=http://localhost:5173,http://localhost:5174,http://localhost:5175

# --- OTP ---------------------------------------------------------------------
OTP_TTL_SECONDS=300
OTP_MAX_PER_HOUR=3
# Returns the OTP in the API response so you can sign in without an SMS
# gateway. This is why the project needs no SMS account. MUST be false in
# production — the app refuses to boot with it on.
OTP_DEBUG_ECHO={debug_flags}

# --- Development sign-in -----------------------------------------------------
# One-click sign-in as a seeded staff account. Also refused in production.
DEV_LOGIN_ENABLED={debug_flags}
DEV_MFA_SECRET=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP

# --- Front ends --------------------------------------------------------------
ADMIN_CONSOLE_PORT=5173
PILGRIM_PWA_PORT=5174
VITE_API_BASE_URL=http://localhost:8000/api/v1
VITE_WS_URL=ws://localhost:8000/ws
# VITE_MAP_STYLE intentionally unset: the console draws its own zone polygons
# on a flat background, so there is no tile CDN and no map key.

# --- AI engine ---------------------------------------------------------------
AI_ENGINE_URL=http://ai-engine:8100
CORE_API_URL=http://core-api:8000
AI_SERVICE_TOKEN={ai_token}

# Simulation is the default source: a full demo with no cameras, no cloud
# vision API and no GPU.  Set to `live` once you have RTSP URLs of your own.
CROWD_SOURCE=sim
SIM_SEED=20260724
SIM_EKADASHI_DATE=2026-07-25
SIM_BASELINE_MULTIPLIER=1.0
# 0 keeps the ~2 GB vision stack (torch, ultralytics, opencv) out of the image.
WITH_VISION=0

# --- Control room contact ----------------------------------------------------
# A phone number, not an API key. Blank is fine; the app then says no number is
# configured rather than showing a button that dials nothing.
CONTROL_ROOM_SMS_NUMBER=

# --- Assistant (OPTIONAL, free tier) -----------------------------------------
# The only external key in the entire project, and it is optional.
#
#   Get one free at https://aistudio.google.com/apikey  (no card required)
#
# Leave it blank and the assistant STILL WORKS: a keyword router answers from
# the same five read-only tools with templated Marathi/English sentences, and
# every turn is logged with outcome `degraded`. With a key you get fluent,
# multi-turn natural language over exactly the same data — the facts are
# identical either way, because both paths read the same tools.
GEMINI_API_KEY={gemini}
GEMINI_MODEL=gemini-2.0-flash
"""


#: Repository root, three levels up from services/core-api/scripts/.
REPO_ROOT = Path(__file__).resolve().parents[3]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=REPO_ROOT / ".env",
        help="Where to write. Defaults to the repository's .env",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .env. Refused by default, because "
        "regenerating PHONE_HASH_SECRET orphans every stored phone hash and "
        "regenerating QR_SIGNING_SECRET invalidates every issued pass.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print instead of writing a file. On Windows, redirecting this "
        "to .env produces a cp1252 file the app cannot parse — prefer the "
        "default.",
    )
    parser.add_argument(
        "--production",
        action="store_true",
        help="Turn off OTP_DEBUG_ECHO and DEV_LOGIN_ENABLED, which the app "
        "refuses to boot with in a production ENVIRONMENT.",
    )
    parser.add_argument(
        "--gemini-key",
        default="",
        help="Optional free Google AI Studio key. Omit to run the assistant "
        "in its deterministic mode.",
    )
    args = parser.parse_args()

    rendered = TEMPLATE.format(
        today=date.today().isoformat(),
        db_password=token(18),
        jwt=token(48),
        phone_hash=token(48),
        qr=token(48),
        ai_token=token(32),
        fernet=Fernet.generate_key().decode(),
        gemini=args.gemini_key,
        debug_flags="false" if args.production else "true",
    )

    if args.stdout:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stdout.write(rendered)
        return 0

    if args.output.exists() and not args.force:
        # ASCII only in console output: a Windows terminal is cp1252 and would
        # render an em-dash as mojibake in the first message a new user sees.
        print(
            f"{args.output} already exists - not overwriting.\n"
            "Regenerating secrets orphans every stored phone hash and invalidates\n"
            "every issued pass. Pass --force if that is genuinely what you want.",
            file=sys.stderr,
        )
        return 1

    # Explicit encoding and newline: the app reads .env as UTF-8, and a CRLF
    # file works fine but a cp1252 one does not.
    args.output.write_text(rendered, encoding="utf-8", newline="\n")

    print(
        f"Wrote {args.output}\n"
        "No external account was used and no key was fetched. Every secret in\n"
        "that file was generated locally just now.\n\n"
        "  Next:  docker compose up -d\n"
        "         docker compose exec core-api python scripts/seed_dev.py\n",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
