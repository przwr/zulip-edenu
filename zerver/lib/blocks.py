# PORTAL EDENU: server-side block visibility helpers.
#
# The portal's block feature keeps MutedUser rows symmetric (hourly
# reconcile), and manual mutes are API-gated off, so a mute row == a
# portal block.  A blocked pair must appear non-existent to each other
# in channels too: no "Muted user" placeholder rows, no reveal button,
# and topics started by either side are entirely invisible.  This
# module computes the exclusion sets the fetch/unread/topic-list/live
# fan-out paths filter with.
#
# Everything here deliberately reads the DB instead of caching, so a
# block applied by the hourly sync hides history on the next fetch
# with no invalidation to wire up.  Portal realms are small; the
# queries below are indexed and bounded by the number of blocked
# users.
# ponytail: per-request queries — cache blocked-topic pairs if a large
# realm ever makes this hot.

import re

from django.conf import settings
from django.db import connection
from sqlalchemy.sql import ClauseElement, and_, func, literal_column, not_
from sqlalchemy.types import Integer, Text

from zerver.lib.muted_users import get_muting_users, get_user_mutes
from zerver.models import MutedUser, UserProfile


def get_portal_blocked_user_ids(user_profile: UserProfile | None) -> set[int]:
    """User ids invisible to this viewer: portal blocks in both directions.

    Realm admins keep full visibility (moderation), and the whole
    feature is a no-op unless PORTAL_EDENU is set.  Returns an empty
    set for spectators/anonymous viewers.
    """
    if not settings.PORTAL_EDENU or user_profile is None or user_profile.is_realm_admin:
        return set()
    muted_by_me = {row["id"] for row in get_user_mutes(user_profile)}
    return muted_by_me | get_muting_users(user_profile.id)


def get_portal_blocked_topic_rows(
    user_profile: UserProfile, blocked_ids: set[int]
) -> set[tuple[int, str]]:
    """(recipient_id, upper-cased topic) pairs the viewer must not see at all.

    A topic counts as "started by" a blocked user when the lowest-id
    message in it (case-insensitive topic identity, like Zulip's
    narrows) was sent by them.  Everything in such a topic — including
    third-party replies — is hidden from the viewer.
    """
    if not blocked_ids:
        return set()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT m.recipient_id, upper(m.subject) AS topic_key
            FROM zerver_message m
            WHERE m.realm_id = %s AND m.is_channel_message AND m.sender_id = ANY(%s)
              AND NOT EXISTS (
                SELECT 1 FROM zerver_message m2
                WHERE m2.recipient_id = m.recipient_id
                  AND upper(m2.subject) = upper(m.subject)
                  AND m2.id < m.id
              )
            """,
            [user_profile.realm_id, list(blocked_ids)],
        )
        return {(row[0], row[1]) for row in cursor.fetchall()}


def get_portal_topic_starter_id(recipient_id: int, topic_name: str) -> int | None:
    """Sender of a topic's first message, for the live fan-out path."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT sender_id FROM zerver_message
            WHERE recipient_id = %s AND upper(subject) = upper(%s) AND is_channel_message
            ORDER BY id LIMIT 1
            """,
            [recipient_id, topic_name],
        )
        row = cursor.fetchone()
    return row[0] if row else None


def get_portal_narrow_conditions(user_profile: UserProfile | None) -> list[ClauseElement]:
    """WHERE clauses excluding blocked content from every message fetch.

    Messages from blocked senders, and every message (including
    third-party replies) in a topic they started, never reach the
    viewer's fetch, search, or permalink results.
    """
    blocked_ids = get_portal_blocked_user_ids(user_profile)
    if not blocked_ids:
        return []
    assert user_profile is not None
    conditions: list[ClauseElement] = [
        literal_column("zerver_message.sender_id", Integer).notin_(blocked_ids)
    ]
    blocked_topic_rows = get_portal_blocked_topic_rows(user_profile, blocked_ids)
    if blocked_topic_rows:
        # why: (recipient_id, upper(subject)) NOT IN pairs, spelled as
        # AND-of-NOTs — SQLAlchemy Core tuple IN rendering is fragile here.
        conditions.extend(
            not_(
                and_(
                    literal_column("zerver_message.recipient_id", Integer) == recipient_id,
                    func.upper(literal_column("zerver_message.subject", Text)) == topic_key,
                )
            )
            for recipient_id, topic_key in blocked_topic_rows
        )
    return conditions


def get_portal_event_hidden_user_ids(
    sender: UserProfile,
    muted_sender_user_ids: set[int],
    *,
    recipient_id: int | None = None,
    topic_name: str | None = None,
) -> set[int]:
    """User ids that must NOT receive a live message event or notification.

    That is anyone in a portal block pair with the message's sender, or —
    for channel messages — with the sender of the topic's first message:
    a topic started by a blocked user stays invisible even when others
    reply in it.  Realm admins always receive.  Runs on every message
    send; the queries are tiny (mute rows + one indexed topic lookup).
    """
    if not settings.PORTAL_EDENU:
        return set()
    hidden = set(muted_sender_user_ids)  # who blocks the sender
    # why: do_send_messages aliases this set for mark-as-read and adds the acting
    # sender's own id — a sender never hides their own message event.
    hidden.discard(sender.id)
    hidden |= {row["id"] for row in get_user_mutes(sender)}  # whom the sender blocks
    if recipient_id is not None and topic_name is not None:
        starter_id = get_portal_topic_starter_id(recipient_id, topic_name)
        if starter_id is not None and starter_id != sender.id:
            hidden |= get_muting_users(starter_id)
            starter = UserProfile.objects.get(id=starter_id)
            hidden |= {row["id"] for row in get_user_mutes(starter)}
    if not hidden:
        return set()
    # Admins are never party to a portal block pair, but never risk
    # hiding events from one (moderation) — filter explicitly.
    admin_ids = set(
        UserProfile.objects.filter(
            id__in=hidden,
            role__in=[UserProfile.ROLE_REALM_ADMINISTRATOR, UserProfile.ROLE_REALM_OWNER],
        ).values_list("id", flat=True)
    )
    return hidden - admin_ids


_BLOCKED_USER_MENTION_RE = re.compile(
    r'<span class="user-mention(?: silent)?" data-user-id="(\d+)">@?[^<]*</span>'
)


_BLOCKED_RAW_MENTION_RE = re.compile(
    r"@(?P<delim>\*\*|_)(?P<name>[^*|]+?)(?:\|(?P<id>\d+))?(?P=delim)"
)
_MENTION_WILDCARD_NAMES = {"all", "everyone", "topic"}


def strip_blocked_user_mentions(
    content: str | None,
    blocked_ids: set[int],
    blocked_names: frozenset[str] = frozenset(),
) -> str | None:
    """Remove mentions of blocked members from message content, per-viewer.

    Mention pills are baked into the per-message rendered HTML at send time and
    live events carry raw markdown, so both forms must be stripped at delivery
    (fetch or event).  HTML pills match by data-user-id; raw tokens by |id|
    suffix when present, otherwise by casefolded full name.  Wildcards never
    match.
    """
    if not content or not blocked_ids:
        return content
    stripped = _BLOCKED_USER_MENTION_RE.sub(
        lambda m: "" if int(m.group(1)) in blocked_ids else m.group(0), content
    )
    if not blocked_names:
        return stripped

    def drop_raw(match: re.Match[str]) -> str:
        if match.group("id") is not None:
            return "" if int(match.group("id")) in blocked_ids else match.group(0)
        name = match.group("name").casefold()
        if name in _MENTION_WILDCARD_NAMES:
            return match.group(0)
        return "" if name in blocked_names else match.group(0)

    return _BLOCKED_RAW_MENTION_RE.sub(drop_raw, stripped)


def get_portal_mention_blocked_map(
    mentioned_user_ids: set[int], viewer_ids: list[int]
) -> dict[int, set[int]]:
    """Map each viewer to the mentioned members they block.

    Third-party messages mentioning a blocked member still reach viewers who
    block them; only the pill must go.  One bulk MutedUser query answers
    viewer x mention for every recipient (rows are the symmetric
    materialization of portal blocks); admins carry no rows, so they keep
    every pill.
    """
    if not settings.PORTAL_EDENU or not mentioned_user_ids or not viewer_ids:
        return {}
    rows = MutedUser.objects.filter(
        user_profile_id__in=viewer_ids, muted_user_id__in=mentioned_user_ids
    ).values_list("user_profile_id", "muted_user_id")
    blocked_map: dict[int, set[int]] = {}
    for viewer_id, blocked_id in rows:
        blocked_map.setdefault(viewer_id, set()).add(blocked_id)
    return blocked_map
