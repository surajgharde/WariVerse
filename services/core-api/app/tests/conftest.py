"""Test fixtures.

Environment is pinned *before* the app is imported, because `Settings` is an
lru_cached singleton — importing first and configuring second would silently
test against the developer's real database.

Integration tests need Postgres (PostGIS + TimescaleDB) and Redis.  Bring them
up with `docker compose up -d db redis`; without them, tests marked `db` or
`redis` skip with a clear reason rather than failing noisily.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

CORE_API_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CORE_API_ROOT))

# psycopg's async mode cannot run on Windows' default ProactorEventLoop.
# Production is Linux, so this only affects local development.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_DEFAULT_DB = "postgresql+psycopg://wariverse:change-me-in-prod@localhost:5432/wariverse"


def _test_database_url() -> str:
    base = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or _DEFAULT_DB
    parts = urlsplit(base)
    name = parts.path.lstrip("/") or "wariverse"
    if not name.endswith("_test"):
        name = f"{name}_test"
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


TEST_DATABASE_URL = _test_database_url()

os.environ.update(
    {
        "ENVIRONMENT": "development",
        "DATABASE_URL": TEST_DATABASE_URL,
        "REDIS_URL": os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15"),
        "JWT_SECRET": "test-jwt-secret-not-used-anywhere-real-000000",
        "PHONE_HASH_SECRET": "test-phone-hash-secret-not-used-anywhere-real",
        "AI_SERVICE_TOKEN": "test-ai-service-token",
        # Must be a real Fernet key: 32 raw bytes, url-safe base64. The obvious
        # "looks like base64" placeholder decodes to 34 bytes and Fernet rejects
        # it — which stays invisible until something actually encrypts, as the
        # incident callback numbers in Phase 5 do.
        "CONTACT_ENCRYPTION_KEY": "dGVzdC1jb250YWN0LWtleS10ZXN0LWNvbnRhY3Qta2U=",
        "OTP_DEBUG_ECHO": "true",
        "LOG_LEVEL": "WARNING",
    }
)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.permissions import Role  # noqa: E402
from app.core.security import hash_password, hash_phone, normalise_phone  # noqa: E402
from app.models import User  # noqa: E402


# ---------------------------------------------------------------------------
# infrastructure availability
# ---------------------------------------------------------------------------
def _admin_url() -> str:
    parts = urlsplit(TEST_DATABASE_URL)
    return urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))


async def _ensure_test_database() -> None:
    """Create the test database if it does not exist, then migrate it."""
    admin = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    db_name = urlsplit(TEST_DATABASE_URL).path.lstrip("/")
    async with admin.connect() as conn:
        exists = await conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db_name})
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    await admin.dispose()

    # Off the event loop: alembic is synchronous and takes a second or two.
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=CORE_API_ROOT,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")


#: CI sets this so a broken service container fails the build instead of
#: quietly turning 19 integration tests into 19 skips.
REQUIRE_INTEGRATION = os.getenv("REQUIRE_INTEGRATION") == "1"


def _unavailable(what: str, exc: Exception) -> None:
    message = f"{what} unavailable for integration tests: {exc}"
    if REQUIRE_INTEGRATION:
        raise RuntimeError(message) from exc
    pytest.skip(message)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def database() -> AsyncIterator[None]:
    """Create and migrate the test database once.

    Safe at session scope because it leaves nothing loop-bound behind: the admin
    engine is disposed and alembic runs in a worker thread.
    """
    try:
        await _ensure_test_database()
    except Exception as exc:
        _unavailable("Postgres", exc)
    yield


@pytest.fixture
async def redis_client(database: None) -> AsyncIterator[object]:
    """Function-scoped on purpose — see the loop-scope note in pyproject.toml."""
    from app.core.redis_client import redis

    try:
        await redis.ping()
    except Exception as exc:
        _unavailable("Redis", exc)
    yield redis
    # Return the connections to the (loop-agnostic) closed state so the next
    # test's loop starts from a clean pool.
    await redis.connection_pool.disconnect(inuse_connections=True)


# ---------------------------------------------------------------------------
# per-test state
# ---------------------------------------------------------------------------
_TRUNCATE_ORDER = (
    "clip_access_log", "purge_log", "breach_events", "pass_notifications", "pass_members",
    "passes", "slots", "incident_events", "missing_persons", "incidents", "responders",
    "assistant_turns", "dindi_pings", "dindi_schedule", "dindis", "halt_towns", "routes",
    "forecasts", "density_readings", "alerts",
    "tripwires", "cameras", "facilities", "gates", "zones", "contact_secrets",
    "audit_log", "users",
)


@pytest.fixture
async def session(database: None, redis_client: object) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        # audit_log has an anti-tamper trigger on UPDATE/DELETE; TRUNCATE is a
        # DDL-level operation and is deliberately still allowed so tests can
        # reset.  Production grants do not include TRUNCATE.
        await conn.execute(text(f"TRUNCATE {', '.join(_TRUNCATE_ORDER)} RESTART IDENTITY CASCADE"))

    from app.core.redis_client import redis

    await redis.flushdb()

    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    from app.core.db import get_session
    from app.main import app

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# user factories
# ---------------------------------------------------------------------------
@pytest.fixture
def make_user(session: AsyncSession):
    async def _make(
        *,
        phone: str,
        name: str = "Test User",
        role: Role = Role.VOLUNTEER,
        password: str | None = "correct-horse-battery-staple",
        is_active: bool = True,
        mfa_secret: str | None = None,
    ) -> User:
        user = User(
            phone=normalise_phone(phone) if role != Role.PILGRIM else None,
            phone_hash=hash_phone(phone),
            name=name,
            role=role,
            language="mr",
            password_hash=hash_password(password) if password else None,
            mfa_secret=mfa_secret,
            is_active=is_active,
        )
        session.add(user)
        await session.commit()
        return user

    return _make


@pytest.fixture
def auth_headers():
    def _headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    return _headers


@pytest.fixture(scope="session")
def api_prefix() -> str:
    return settings.api_v1_prefix
