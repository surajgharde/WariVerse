"""The Wari heritage archive (Track 1, item 5).

The rules pinned here are the ones that make this an archive rather than a wall:

* nothing a pilgrim submits is published until a human publishes it;
* a client cannot publish its own contribution by sending a status;
* declining requires a reason the contributor can read;
* a declined text is kept, not destroyed;
* an unpublished item cannot be fished out by walking ids;
* Marathi is required and English is not.

Needs Postgres and Redis (`docker compose up -d db redis`).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Role
from app.core.security import create_access_token
from app.models import AuditLog, HeritageItem
from app.models.heritage import ReviewState
from app.services.audit_service import AuditAction

pytestmark = [pytest.mark.db, pytest.mark.redis]

ABHANG = "सुंदर ते ध्यान उभे विटेवरी । कर कटावरी ठेवूनिया ॥"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def pilgrim_token(make_user):
    user = await make_user(phone="9876543210", role=Role.PILGRIM, password=None, name="यात्रेकरू")
    token, _ = create_access_token(subject=str(user.id), role=user.role)
    return token


@pytest.fixture
async def other_pilgrim_token(make_user):
    user = await make_user(phone="9811111111", role=Role.PILGRIM, password=None, name="दुसरा")
    token, _ = create_access_token(subject=str(user.id), role=user.role)
    return token


@pytest.fixture
async def admin_token(make_user):
    user = await make_user(phone="9820044556", role=Role.ADMINISTRATOR, name="प्रशासक")
    token, _ = create_access_token(subject=str(user.id), role=user.role, mfa_verified=True)
    return token


async def contribute(client: AsyncClient, api_prefix: str, token: str, **overrides) -> dict:
    body = {
        "kind": "abhang",
        "title_mr": "सुंदर ते ध्यान",
        "body_mr": ABHANG,
        "attribution": "संत तुकाराम",
        "source": "गाथा",
        "era": "१७वे शतक",
        **overrides,
    }
    response = await client.post(f"{api_prefix}/heritage", json=body, headers=bearer(token))
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# the moderation gate
# ---------------------------------------------------------------------------
async def test_a_contribution_is_pending_and_invisible_until_published(
    client: AsyncClient, api_prefix: str, pilgrim_token: str
) -> None:
    body = await contribute(client, api_prefix, pilgrim_token)
    assert body["status"] == "pending"
    assert body["published_at"] is None

    public = await client.get(f"{api_prefix}/heritage")
    assert public.status_code == 200, public.text
    assert public.json()["total"] == 0, "the gate fails closed"


async def test_a_client_cannot_publish_its_own_contribution(
    client: AsyncClient, api_prefix: str, pilgrim_token: str
) -> None:
    """The status is set server-side and the schema has no field for it."""
    body = await contribute(
        client,
        api_prefix,
        pilgrim_token,
        status="published",
        published_at="2020-01-01T00:00:00Z",
        reviewed_by=str(uuid.uuid4()),
    )
    assert body["status"] == "pending"
    assert body["published_at"] is None


async def test_a_pilgrim_cannot_moderate(
    client: AsyncClient, api_prefix: str, pilgrim_token: str
) -> None:
    item = await contribute(client, api_prefix, pilgrim_token)

    queue = await client.get(f"{api_prefix}/heritage/review/queue", headers=bearer(pilgrim_token))
    assert queue.status_code == 403

    review = await client.post(
        f"{api_prefix}/heritage/{item['id']}/review",
        json={"publish": True},
        headers=bearer(pilgrim_token),
    )
    assert review.status_code == 403


async def test_publishing_makes_it_readable_without_signing_in(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, admin_token: str
) -> None:
    """A Warkari at a halt town should not need an account to read why it halts."""
    item = await contribute(client, api_prefix, pilgrim_token)

    published = await client.post(
        f"{api_prefix}/heritage/{item['id']}/review",
        json={"publish": True},
        headers=bearer(admin_token),
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"

    # No Authorization header at all.
    public = await client.get(f"{api_prefix}/heritage")
    assert public.status_code == 200
    items = public.json()["items"]
    assert len(items) == 1
    assert items[0]["body_mr"] == ABHANG
    assert items[0]["attribution"] == "संत तुकाराम"
    # The public shape carries no review state.
    assert "review_note" not in items[0]
    assert "status" not in items[0]


async def test_declining_requires_a_reason_and_keeps_the_text(
    client: AsyncClient,
    api_prefix: str,
    session: AsyncSession,
    pilgrim_token: str,
    admin_token: str,
) -> None:
    item = await contribute(client, api_prefix, pilgrim_token)

    bare = await client.post(
        f"{api_prefix}/heritage/{item['id']}/review",
        json={"publish": False},
        headers=bearer(admin_token),
    )
    assert bare.status_code == 400
    assert bare.json()["error"]["code"] == "REVIEW_NOTE_REQUIRED"

    declined = await client.post(
        f"{api_prefix}/heritage/{item['id']}/review",
        json={"publish": False, "note": "स्रोत तपासता आला नाही"},
        headers=bearer(admin_token),
    )
    assert declined.status_code == 200, declined.text
    assert declined.json()["status"] == "rejected"

    # The only copy anybody typed out is still there.
    row = await session.get(HeritageItem, uuid.UUID(item["id"]))
    await session.refresh(row)
    assert row.body_mr == ABHANG
    assert row.review_note == "स्रोत तपासता आला नाही"


async def test_a_contributor_learns_the_fate_of_their_own_submission(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, admin_token: str
) -> None:
    item = await contribute(client, api_prefix, pilgrim_token)
    await client.post(
        f"{api_prefix}/heritage/{item['id']}/review",
        json={"publish": False, "note": "स्रोत तपासता आला नाही"},
        headers=bearer(admin_token),
    )

    mine = await client.get(f"{api_prefix}/heritage/mine", headers=bearer(pilgrim_token))
    assert mine.status_code == 200, mine.text
    entries = mine.json()
    assert len(entries) == 1
    assert entries[0]["status"] == "rejected"
    assert entries[0]["review_note"] == "स्रोत तपासता आला नाही"


async def test_an_unpublished_item_cannot_be_fished_out_by_walking_ids(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, other_pilgrim_token: str
) -> None:
    """404 rather than 403: confirming the id exists is itself the answer sought."""
    item = await contribute(client, api_prefix, pilgrim_token)

    response = await client.get(
        f"{api_prefix}/heritage/{item['id']}", headers=bearer(other_pilgrim_token)
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "HERITAGE_NOT_FOUND"


async def test_publishing_is_audited_with_who_did_it(
    client: AsyncClient, api_prefix: str, session: AsyncSession, pilgrim_token: str, admin_token: str
) -> None:
    """Publishing puts text under the archive's name — a decision with a name on it."""
    item = await contribute(client, api_prefix, pilgrim_token)
    await client.post(
        f"{api_prefix}/heritage/{item['id']}/review",
        json={"publish": True},
        headers=bearer(admin_token),
    )

    entry = await session.scalar(
        select(AuditLog).where(AuditLog.action == AuditAction.HERITAGE_REVIEWED)
    )
    assert entry is not None
    assert entry.actor_id is not None
    assert entry.meta["status"] == ReviewState.PUBLISHED


# ---------------------------------------------------------------------------
# the archive itself
# ---------------------------------------------------------------------------
async def test_marathi_is_required_and_english_is_not(
    client: AsyncClient, api_prefix: str, pilgrim_token: str
) -> None:
    """An archive whose canonical text is a translation has lost the thing itself."""
    without_marathi = await client.post(
        f"{api_prefix}/heritage",
        json={"kind": "abhang", "title_mr": "शीर्षक", "body_en": "English only"},
        headers=bearer(pilgrim_token),
    )
    assert without_marathi.status_code == 422

    without_english = await contribute(client, api_prefix, pilgrim_token)
    assert without_english["body_en"] is None


async def test_the_archive_filters_by_kind_and_searches_titles(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, admin_token: str
) -> None:
    first = await contribute(client, api_prefix, pilgrim_token)
    second = await contribute(
        client,
        api_prefix,
        pilgrim_token,
        kind="place_lore",
        title_mr="वाखरी येथील रिंगण",
        body_mr="पालखी येथे थांबते कारण…",
        attribution=None,
    )
    for item in (first, second):
        await client.post(
            f"{api_prefix}/heritage/{item['id']}/review",
            json={"publish": True},
            headers=bearer(admin_token),
        )

    by_kind = await client.get(f"{api_prefix}/heritage?kind=place_lore")
    assert by_kind.json()["total"] == 1
    assert by_kind.json()["items"][0]["title_mr"] == "वाखरी येथील रिंगण"

    by_text = await client.get(f"{api_prefix}/heritage?q=तुकाराम")
    assert by_text.json()["total"] == 1
    assert by_text.json()["items"][0]["attribution"] == "संत तुकाराम"


async def test_a_contribution_credits_the_person_it_came_from(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, admin_token: str
) -> None:
    """A grandson submitting his grandmother's ovi should be able to name her."""
    item = await contribute(
        client,
        api_prefix,
        pilgrim_token,
        kind="ovi",
        title_mr="जात्यावरची ओवी",
        body_mr="पहिली माझी ओवी ग विठ्ठलाला ।",
        contributed_by_name="सखुबाई पवार",
    )
    await client.post(
        f"{api_prefix}/heritage/{item['id']}/review",
        json={"publish": True},
        headers=bearer(admin_token),
    )

    public = await client.get(f"{api_prefix}/heritage?kind=ovi")
    assert public.json()["items"][0]["contributed_by_name"] == "सखुबाई पवार"


async def test_a_moderator_can_correct_without_unpublishing(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, admin_token: str
) -> None:
    """Fixing a typo must not pull the page out from under whoever is reading it."""
    item = await contribute(client, api_prefix, pilgrim_token)
    await client.post(
        f"{api_prefix}/heritage/{item['id']}/review",
        json={"publish": True},
        headers=bearer(admin_token),
    )

    corrected = await client.patch(
        f"{api_prefix}/heritage/{item['id']}",
        json={"source": "तुकाराम गाथा, अभंग ७२३"},
        headers=bearer(admin_token),
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["status"] == "published"
    assert corrected.json()["source"] == "तुकाराम गाथा, अभंग ७२३"


async def test_the_review_queue_is_oldest_first(
    client: AsyncClient, api_prefix: str, pilgrim_token: str, admin_token: str
) -> None:
    """A contribution that has waited a week is the one to read next."""
    first = await contribute(client, api_prefix, pilgrim_token, title_mr="पहिला")
    await contribute(client, api_prefix, pilgrim_token, title_mr="दुसरा")

    queue = await client.get(f"{api_prefix}/heritage/review/queue", headers=bearer(admin_token))
    assert queue.status_code == 200, queue.text
    assert queue.json()["total"] == 2
    assert queue.json()["items"][0]["id"] == first["id"]
