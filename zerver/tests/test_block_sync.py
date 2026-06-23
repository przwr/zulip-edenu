# PORTAL EDENU: tests for the user-block → MutedUser reconcile in
# sync_reputation_to_zulip, and the PORTAL_EDENU gate on the mute API.
from typing import Any
from unittest.mock import patch

import orjson
from django.test import override_settings
from typing_extensions import override

from zerver.actions.create_user import do_create_user
from zerver.actions.muted_users import do_mute_user
from zerver.actions.reactions import do_add_reaction
from zerver.actions.submessage import do_add_submessage
from zerver.actions.users import do_deactivate_user
from zerver.lib.blocks import (
    get_portal_blocked_topic_rows,
    get_portal_event_hidden_user_ids,
    get_portal_mention_blocked_map,
    strip_blocked_user_mentions,
)
from zerver.lib.message import get_raw_unread_data
from zerver.lib.muted_users import get_muting_users, get_user_mutes
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.test_helpers import queries_captured
from zerver.lib.topic import DB_TOPIC_NAME
from zerver.lib.users import get_user_dicts_in_realm
from zerver.management.commands.sync_reputation_to_zulip import Command
from zerver.models import Message, MutedUser, Reaction, UserProfile
from zerver.models.realms import get_realm


class BlockSyncTest(ZulipTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.realm = get_realm("zulip")  # why: the default test realm seeded by ZulipTestCase
        self.admin = self.example_user("iago")
        self.alice = do_create_user(
            "block-alice@zulip.com",
            "password",
            self.realm,
            "Block Alice",
            acting_user=self.admin,
        )
        self.bob = do_create_user(
            "block-bob@zulip.com",
            "password",
            self.realm,
            "Block Bob",
            acting_user=self.admin,
        )
        self.carol = do_create_user(
            "block-carol@zulip.com",
            "password",
            self.realm,
            "Block Carol",
            acting_user=self.admin,
        )
        self.cmd = Command()
        self.cmd.configure_logging(verbose=False)

    def _snapshot(self, entries: list[tuple[str, str]]) -> dict[str, Any]:
        return {
            "users": [],
            "blocks": [{"blocker": blocker, "blocked": blocked} for blocker, blocked in entries],
        }

    def _current_pairs(self) -> set[tuple[int, int]]:
        return set(MutedUser.objects.values_list("user_profile_id", "muted_user_id"))

    def test_single_directed_block_creates_symmetric_mutes(self) -> None:
        # A→B in the portal is enough: both directions are muted.
        with self.assertLogs("reputation_sync", "INFO"):
            self.cmd.reconcile_blocks(
                self._snapshot([("block-alice@zulip.com", "block-bob@zulip.com")]),
                self.admin,
                dry_run=False,
            )
        self.assertEqual(
            self._current_pairs(), {(self.alice.id, self.bob.id), (self.bob.id, self.alice.id)}
        )
        self.assertEqual(get_muting_users(self.bob.id), {self.alice.id})

    def test_removed_block_unmutes_both_sides(self) -> None:
        self.cmd.reconcile_blocks(
            self._snapshot([("block-alice@zulip.com", "block-bob@zulip.com")]),
            self.admin,
            dry_run=False,
        )
        self.cmd.reconcile_blocks(self._snapshot([]), self.admin, dry_run=False)
        self.assertEqual(self._current_pairs(), set())
        self.assertEqual(get_muting_users(self.bob.id), set())
        self.assertEqual(get_user_mutes(self.alice), [])

    def test_keeps_existing_rows_and_adds_new_pair(self) -> None:
        self.cmd.reconcile_blocks(
            self._snapshot([("block-alice@zulip.com", "block-bob@zulip.com")]),
            self.admin,
            dry_run=False,
        )
        # Idempotent: same snapshot again changes nothing.
        with queries_captured():
            self.cmd.reconcile_blocks(
                self._snapshot([("block-alice@zulip.com", "block-bob@zulip.com")]),
                self.admin,
                dry_run=False,
            )
        # A second pair is added without touching the first.
        self.cmd.reconcile_blocks(
            self._snapshot(
                [
                    ("block-alice@zulip.com", "block-bob@zulip.com"),
                    ("block-carol@zulip.com", "block-alice@zulip.com"),
                ]
            ),
            self.admin,
            dry_run=False,
        )
        self.assertEqual(
            self._current_pairs(),
            {
                (self.alice.id, self.bob.id),
                (self.bob.id, self.alice.id),
                (self.carol.id, self.alice.id),
                (self.alice.id, self.carol.id),
            },
        )

    def test_unknown_or_service_or_inactive_users_skipped(self) -> None:
        do_deactivate_user(self.carol, acting_user=self.admin)
        with self.assertLogs("reputation_sync", "INFO"):
            self.cmd.reconcile_blocks(
                self._snapshot(
                    [
                        ("nobody@zulip.com", "block-alice@zulip.com"),
                        ("portal@edenu.pl", "block-alice@zulip.com"),
                        ("block-carol@zulip.com", "block-alice@zulip.com"),
                        ("block-alice@zulip.com", "block-alice@zulip.com"),
                    ]
                ),
                self.admin,
                dry_run=False,
            )
        self.assertEqual(self._current_pairs(), set())

    def test_dry_run_makes_no_changes(self) -> None:
        with self.assertLogs("reputation_sync", "INFO"):
            self.cmd.reconcile_blocks(
                self._snapshot([("block-alice@zulip.com", "block-bob@zulip.com")]),
                self.admin,
                dry_run=True,
            )
        self.assertEqual(self._current_pairs(), set())

    def test_stale_mute_row_not_in_portal_is_deleted(self) -> None:
        # In prod the mute API is gated off, so any pre-existing mute row is
        # portal-owned; the reconcile must remove it when the portal drops it.
        do_mute_user(self.alice, self.carol)
        self.assertEqual(self._current_pairs(), {(self.alice.id, self.carol.id)})
        self.cmd.reconcile_blocks(
            self._snapshot([("block-alice@zulip.com", "block-bob@zulip.com")]),
            self.admin,
            dry_run=False,
        )
        self.assertEqual(
            self._current_pairs(), {(self.alice.id, self.bob.id), (self.bob.id, self.alice.id)}
        )


class MuteApiGateTest(ZulipTestCase):
    def test_mute_rejected_when_portal_edenu(self) -> None:
        hamlet = self.example_user("hamlet")
        othello = self.example_user("othello")
        with override_settings(PORTAL_EDENU=True):
            result = self.api_post(
                hamlet,
                "/api/v1/users/me/muted_users/" + str(othello.id),
            )
            self.assert_json_error(result, "Muting is managed by Portal Edenu")
        self.assertIsNone(MutedUser.objects.filter(user_profile=hamlet, muted_user=othello).first())

    def test_unmute_rejected_when_portal_edenu(self) -> None:
        hamlet = self.example_user("hamlet")
        othello = self.example_user("othello")
        with override_settings(PORTAL_EDENU=False):
            do_mute_user(hamlet, othello)
        with override_settings(PORTAL_EDENU=True):
            result = self.api_delete(
                hamlet,
                "/api/v1/users/me/muted_users/" + str(othello.id),
            )
            self.assert_json_error(result, "Muting is managed by Portal Edenu")
        self.assertIsNotNone(
            MutedUser.objects.filter(user_profile=hamlet, muted_user=othello).first()
        )


class BlockedPairVisibilityTest(ZulipTestCase):
    """PORTAL EDENU: directory filter and DM rejection for blocked pairs."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.realm = get_realm("zulip")
        self.admin = self.example_user("iago")
        self.alice = do_create_user(
            "vis-alice@zulip.com", "password", self.realm, "Vis Alice", acting_user=self.admin
        )
        self.bob = do_create_user(
            "vis-bob@zulip.com", "password", self.realm, "Vis Bob", acting_user=self.admin
        )
        # Symmetric block, exactly what the hourly reconcile writes.
        do_mute_user(self.alice, self.bob)
        do_mute_user(self.bob, self.alice)

    def _member_ids(self, requesting_user: UserProfile) -> set[int]:
        accessible, _inaccessible = get_user_dicts_in_realm(self.realm, requesting_user)
        return {d["id"] for d in accessible}

    def test_directory_hides_blocked_pair_for_member(self) -> None:
        with override_settings(PORTAL_EDENU=True):
            ids = self._member_ids(self.alice)
            self.assertNotIn(self.bob.id, ids)
            self.assertIn(self.alice.id, ids)  # self stays visible
            # Bot + admin remain visible.
            self.assertIn(self.example_user("hamlet").id, ids)

    def test_directory_keeps_everything_for_admin(self) -> None:
        with override_settings(PORTAL_EDENU=True):
            ids = self._member_ids(self.admin)
            self.assertIn(self.alice.id, ids)
            self.assertIn(self.bob.id, ids)

    def test_directory_unfiltered_when_not_portal_edenu(self) -> None:
        with override_settings(PORTAL_EDENU=False):
            ids = self._member_ids(self.alice)
            self.assertIn(self.bob.id, ids)

    def test_dm_between_blocked_pair_rejected(self) -> None:
        with override_settings(PORTAL_EDENU=True):
            result = self.api_post(
                self.alice,
                "/api/v1/messages",
                {
                    "type": "private",
                    "to": orjson.dumps([self.bob.id]).decode(),
                    "content": "hello",
                },
            )
            self.assert_json_error(result, "You cannot send direct messages to this user.")

    def test_dm_allowed_when_not_portal_edenu(self) -> None:
        with override_settings(PORTAL_EDENU=False):
            result = self.api_post(
                self.alice,
                "/api/v1/messages",
                {
                    "type": "private",
                    "to": orjson.dumps([self.bob.id]).decode(),
                    "content": "hello",
                },
            )
            self.assert_json_success(result)

    def test_dm_to_others_still_works(self) -> None:
        hamlet = self.example_user("hamlet")
        with override_settings(PORTAL_EDENU=True):
            result = self.api_post(
                self.alice,
                "/api/v1/messages",
                {
                    "type": "private",
                    "to": orjson.dumps([hamlet.id]).decode(),
                    "content": "hello",
                },
            )
            self.assert_json_success(result)


class BlockVisibilityTest(ZulipTestCase):
    """PORTAL EDENU: a blocked pair never sees each other's messages — not as
    muted placeholders, not at all — and topics started by either side are
    invisible, including third-party replies in them."""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.realm = get_realm("zulip")
        self.admin = self.example_user("iago")
        self.alice = do_create_user(
            "block-alice@zulip.com", "password", self.realm, "Block Alice", acting_user=self.admin
        )
        self.bob = do_create_user(
            "block-bob@zulip.com", "password", self.realm, "Block Bob", acting_user=self.admin
        )
        self.carol = do_create_user(
            "block-carol@zulip.com", "password", self.realm, "Block Carol", acting_user=self.admin
        )
        self.stream = self.make_stream("block-stream", realm=self.realm)
        for user in (self.alice, self.bob, self.carol):
            self.subscribe(user, self.stream.name)

    def _block_pair(self) -> None:
        # why: symmetric rows, exactly the state the hourly reconcile leaves.
        do_mute_user(self.alice, self.bob)
        do_mute_user(self.bob, self.alice)

    def _fetch_stream_messages(self, user: Any) -> list[dict[str, Any]]:
        result = self.api_get(
            user,
            "/api/v1/messages",
            {
                "anchor": 1,
                "num_before": 0,
                "num_after": 1000,
                "narrow": orjson.dumps(
                    [{"operator": "stream", "operand": self.stream.name}]
                ).decode(),
            },
        )
        return self.assert_json_success(result)["messages"]

    def _topic_names(self, user: Any) -> list[str]:
        result = self.api_get(user, f"/api/v1/users/me/{self.stream.id}/topics")
        return [row["name"] for row in self.assert_json_success(result)["topics"]]

    def test_fetch_hides_blocked_senders_retroactively(self) -> None:
        self.send_stream_message(self.alice, self.stream.name, "from alice", topic_name="t-a")
        self.send_stream_message(self.carol, self.stream.name, "from carol", topic_name="t-c")
        self._block_pair()

        with override_settings(PORTAL_EDENU=True):
            bob_senders = {m["sender_id"] for m in self._fetch_stream_messages(self.bob)}
            admin_senders = {m["sender_id"] for m in self._fetch_stream_messages(self.admin)}

        self.assertNotIn(self.alice.id, bob_senders)
        self.assertIn(self.carol.id, bob_senders)
        # admins keep full visibility for moderation
        self.assertIn(self.alice.id, admin_senders)

    def test_topic_started_by_blocked_user_fully_invisible(self) -> None:
        self.send_stream_message(
            self.alice, self.stream.name, "alice starts", topic_name="alice-topic"
        )
        carol_reply_id = self.send_stream_message(
            self.carol, self.stream.name, "carol replies", topic_name="alice-topic"
        )
        self.send_stream_message(self.bob, self.stream.name, "bob starts", topic_name="bob-topic")

        self._block_pair()

        with override_settings(PORTAL_EDENU=True):
            bob_messages = self._fetch_stream_messages(self.bob)
            bob_topics = self._topic_names(self.bob)
            admin_topics = self._topic_names(self.admin)

        # the whole topic is gone for bob — alice's opener AND carol's reply
        bob_topics_in_view = {m[DB_TOPIC_NAME] for m in bob_messages}
        self.assertNotIn("alice-topic", bob_topics_in_view)
        self.assertNotIn(carol_reply_id, {m["id"] for m in bob_messages})
        self.assertIn("bob-topic", bob_topics_in_view)
        self.assertNotIn("alice-topic", bob_topics)
        self.assertIn("bob-topic", bob_topics)
        self.assertIn("alice-topic", admin_topics)

    def test_blocked_content_visible_when_feature_off(self) -> None:
        self.send_stream_message(self.alice, self.stream.name, "from alice", topic_name="t-a")
        self._block_pair()

        with override_settings(PORTAL_EDENU=False):
            senders = {m["sender_id"] for m in self._fetch_stream_messages(self.bob)}

        self.assertIn(self.alice.id, senders)

    def test_dm_history_hidden_after_block(self) -> None:
        self.send_personal_message(self.alice, self.bob, "old dm")
        self._block_pair()

        with override_settings(PORTAL_EDENU=True):
            result = self.api_get(
                self.bob,
                "/api/v1/messages",
                {
                    "anchor": 1,
                    "num_before": 0,
                    "num_after": 1000,
                    "narrow": orjson.dumps([{"operator": "is", "operand": "dm"}]).decode(),
                },
            )
            senders = {m["sender_id"] for m in self.assert_json_success(result)["messages"]}

        self.assertNotIn(self.alice.id, senders)

    def test_unread_counts_exclude_blocked_topics(self) -> None:
        self._block_pair()
        with override_settings(PORTAL_EDENU=True):
            self.send_stream_message(
                self.alice, self.stream.name, "alice starts", topic_name="hidden-topic"
            )
            carol_reply_id = self.send_stream_message(
                self.carol, self.stream.name, "carol replies", topic_name="hidden-topic"
            )
            carol_own_id = self.send_stream_message(
                self.carol, self.stream.name, "carol own topic", topic_name="visible-topic"
            )
            raw_unread = get_raw_unread_data(self.bob)

        unread_ids = (
            set(raw_unread["stream_dict"])
            | set(raw_unread["pm_dict"])
            | set(raw_unread["huddle_dict"])
        )
        # alice's own message arrives flagged read (muted sender); the topic
        # filter must still hide carol's reply inside the blocked topic
        self.assertNotIn(carol_reply_id, unread_ids)
        self.assertIn(carol_own_id, unread_ids)

    def test_no_live_event_to_blocked_viewer(self) -> None:
        # why: the fan-out decision is what keeps the muted placeholder (and
        # the event) away from the blocked viewer; exercised directly since
        # backend tests have no live tornado queues.
        self._block_pair()
        with override_settings(PORTAL_EDENU=True):
            hidden = get_portal_event_hidden_user_ids(self.alice, get_muting_users(self.alice.id))
            self.assertEqual(hidden, {self.bob.id})

            # carol replying in an alice-started topic still hides from bob
            self.send_stream_message(
                self.alice, self.stream.name, "alice starts", topic_name="t-live"
            )
            hidden_topic = get_portal_event_hidden_user_ids(
                self.carol,
                set(),
                recipient_id=self.stream.recipient_id,
                topic_name="t-live",
            )
            self.assertIn(self.bob.id, hidden_topic)

            # carol's own topic is unaffected
            hidden_fresh = get_portal_event_hidden_user_ids(
                self.carol,
                set(),
                recipient_id=self.stream.recipient_id,
                topic_name="t-carol",
            )
            self.assertNotIn(self.bob.id, hidden_fresh)

        with override_settings(PORTAL_EDENU=False):
            hidden_off = get_portal_event_hidden_user_ids(
                self.alice, get_muting_users(self.alice.id)
            )
            self.assertEqual(hidden_off, set())

    def test_admin_never_hidden_even_if_muted(self) -> None:
        # portal never blocks the admin, but a stray mute row pointing at one
        # must not hide events from moderation
        do_mute_user(self.admin, self.bob)
        with override_settings(PORTAL_EDENU=True):
            hidden = get_portal_event_hidden_user_ids(self.bob, get_muting_users(self.bob.id))
        self.assertEqual(hidden, set())

    def test_blocked_votes_and_reactions_hidden_in_fetch(self) -> None:
        # why: a blocked user's poll votes (submessages) and reactions
        # ride along inside third-party messages; the strip in
        # messages_for_ids must drop them for members but not admins.
        self._block_pair()
        carol_msg = self.send_stream_message(
            self.carol, self.stream.name, "/poll Lunch?\nTacos\nSushi", topic_name="t-poll"
        )
        do_add_submessage(
            self.realm,
            self.alice.id,
            carol_msg,
            "vote",
            orjson.dumps({"key": "1", "vote": 1}).decode(),
        )
        do_add_submessage(
            self.realm,
            self.carol.id,
            carol_msg,
            "vote",
            orjson.dumps({"key": "0", "vote": 1}).decode(),
        )
        do_add_reaction(
            self.alice,
            Message.objects.get(id=carol_msg),
            "octopus",
            "1f419",
            Reaction.UNICODE_EMOJI,
        )

        with override_settings(PORTAL_EDENU=True):
            bob_msgs = self._fetch_stream_messages(self.bob)
            admin_msgs = self._fetch_stream_messages(self.admin)
        poll_bob = next(m for m in bob_msgs if m["id"] == carol_msg)
        poll_admin = next(m for m in admin_msgs if m["id"] == carol_msg)
        self.assertEqual({s["sender_id"] for s in poll_bob["submessages"]}, {self.carol.id})
        self.assertEqual(
            {s["sender_id"] for s in poll_admin["submessages"]},
            {self.alice.id, self.carol.id},
        )
        self.assertEqual(poll_bob["reactions"], [])
        self.assert_length(poll_admin["reactions"], 1)

    def test_blocked_content_visible_when_feature_off_polls(self) -> None:
        carol_msg = self.send_stream_message(
            self.carol, self.stream.name, "/poll Lunch?\nTacos\nSushi", topic_name="t-poll2"
        )
        do_add_submessage(
            self.realm,
            self.alice.id,
            carol_msg,
            "vote",
            orjson.dumps({"key": "1", "vote": 1}).decode(),
        )
        self._block_pair()
        with override_settings(PORTAL_EDENU=False):
            bob_msgs = self._fetch_stream_messages(self.bob)
        poll = next(m for m in bob_msgs if m["id"] == carol_msg)
        # feature off: the widget submessage (carol) and alice's vote both show
        self.assertEqual(
            {s["sender_id"] for s in poll["submessages"]}, {self.alice.id, self.carol.id}
        )

    def test_dm_gate_skips_bots(self) -> None:
        # why: bots can't be portal-blocked; the gate must `continue`
        # past them rather than consult (or fail on) mute rows.
        self._block_pair()
        bot = self.create_test_bot("gatebot", self.admin)
        with override_settings(PORTAL_EDENU=True):
            result = self.api_post(
                self.bob,
                "/api/v1/messages",
                {
                    "type": "private",
                    "to": orjson.dumps([bot.id]).decode(),
                    "content": "hello bot",
                },
            )
            self.assert_json_success(result)

    def test_blocked_topic_rows_empty_set_short_circuits(self) -> None:
        # why: the helper's early exit for an empty blocked set is the
        # no-op path every non-member fetch takes.
        self.assertEqual(get_portal_blocked_topic_rows(self.bob, set()), set())

    @override_settings(PORTAL_EDENU=True)
    def test_third_party_mention_pill_hidden_on_fetch(self) -> None:
        self._block_pair()
        self.send_stream_message(self.carol, self.stream.name, "@**Block Alice** ping")

        bob_content = self._fetch_stream_messages(self.bob)[0]["content"]
        self.assertNotIn("user-mention", bob_content)
        self.assertNotIn("Block Alice", bob_content)

        # why: only viewers who block the mentioned member lose the pill
        carol_content = self._fetch_stream_messages(self.carol)[0]["content"]
        self.assertIn("user-mention", carol_content)
        self.assertIn("Block Alice", carol_content)

    @override_settings(PORTAL_EDENU=True)
    def test_third_party_mention_pill_hidden_in_live_event(self) -> None:
        self._block_pair()
        with patch("zerver.actions.message_send.send_event_on_commit") as mock_send:
            self.send_stream_message(self.carol, self.stream.name, "@**Block Alice** ping")

        message_sends = [
            call
            for call in mock_send.call_args_list
            if len(call.args) > 1 and call.args[1].get("type") == "message"
        ]
        self.assert_length(message_sends, 2)

        bob_events = [c for c in message_sends if any(u["id"] == self.bob.id for u in c.args[2])]
        self.assert_length(bob_events, 1)
        self.assertNotIn("Block Alice", bob_events[0].args[1]["message_dict"]["content"])

        carol_events = [
            c for c in message_sends if any(u["id"] == self.carol.id for u in c.args[2])
        ]
        self.assert_length(carol_events, 1)
        self.assertIn("Block Alice", carol_events[0].args[1]["message_dict"]["content"])


class MentionStripHelpersTest(ZulipTestCase):
    """PORTAL EDENU: unit coverage for the mention-pill strip primitives."""

    def test_strip_blocked_user_mentions_removes_only_blocked_pills(self) -> None:
        content = (
            "<p>hey "
            '<span class="user-mention" data-user-id="42">@Blocked Name</span> and '
            '<span class="user-mention" data-user-id="99">@Other Name</span> and '
            '<span class="user-mention silent" data-user-id="42">Blocked Name</span> and '
            '<span class="user-mention channel-wildcard-mention" data-user-id="*">@**all**</span>'
            "</p>"
        )
        stripped = strip_blocked_user_mentions(content, {42})
        assert stripped is not None
        self.assertNotIn("Blocked Name", stripped)
        self.assertIn("@Other Name", stripped)
        self.assertIn("channel-wildcard-mention", stripped)

    def test_strip_blocked_user_mentions_raw_tokens(self) -> None:
        names = frozenset({"blocked name"})
        content = (
            "hey @**Blocked Name|42** and @**Other Name|99** and "
            "@_Blocked Name_ and @_blocked name_ and @**all** and @**topic**"
        )
        stripped = strip_blocked_user_mentions(content, {42}, names)
        assert stripped is not None
        self.assertNotIn("Blocked", stripped)
        self.assertIn("@**Other Name|99**", stripped)
        self.assertIn("@**all**", stripped)
        self.assertIn("@**topic**", stripped)

    def test_strip_blocked_user_mentions_passthrough(self) -> None:
        self.assertIsNone(strip_blocked_user_mentions(None, {42}))
        self.assertEqual(strip_blocked_user_mentions("<p>hi</p>", set()), "<p>hi</p>")

    @override_settings(PORTAL_EDENU=True)
    def test_mention_blocked_map_is_per_viewer(self) -> None:
        realm = get_realm("zulip")
        alice = do_create_user(
            "mention-alice@zulip.com", "password", realm, "Mention Alice", acting_user=None
        )
        bob = do_create_user(
            "mention-bob@zulip.com", "password", realm, "Mention Bob", acting_user=None
        )
        carol = do_create_user(
            "mention-carol@zulip.com", "password", realm, "Mention Carol", acting_user=None
        )
        do_mute_user(bob, alice)
        do_mute_user(alice, bob)

        self.assertEqual(
            get_portal_mention_blocked_map({alice.id, carol.id}, [bob.id, carol.id]),
            {bob.id: {alice.id}},
        )
        with override_settings(PORTAL_EDENU=False):
            self.assertEqual(get_portal_mention_blocked_map({alice.id}, [bob.id]), {})
