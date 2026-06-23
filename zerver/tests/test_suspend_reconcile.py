# PORTAL EDENU: tests for the suspension reconciler in sync_reputation_to_zulip.
from typing import Any

from typing_extensions import override

from zerver.actions.create_user import do_create_user
from zerver.actions.users import change_user_is_active
from zerver.lib.test_classes import ZulipTestCase
from zerver.management.commands.sync_reputation_to_zulip import Command
from zerver.models.realms import get_realm


class SuspendReconcileTest(ZulipTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.realm = get_realm("zulip")  # why: the default test realm seeded by ZulipTestCase
        self.admin = self.example_user("iago")
        self.target = do_create_user(
            "reconcile-target@zulip.com",
            "password",
            self.realm,
            "Reconcile Target",
            acting_user=self.admin,
        )
        self.cmd = Command()
        self.cmd.configure_logging(verbose=False)

    def _row(self, email: str, is_active: bool) -> dict[str, Any]:
        return {
            "email": email,
            "portal_uuid": "unused",
            "rank": "Normal",
            "total_points": 0,
            "activity_points": 0,
            "rating_points": 0,
            "wiek": "",
            "is_active": is_active,
        }

    def test_deactivates_when_authentik_inactive(self) -> None:
        self.assertTrue(self.target.is_active)
        with self.assertLogs("reputation_sync", "INFO"):
            self.cmd.reconcile_active_state(
                [self._row("reconcile-target@zulip.com", False)], self.admin
            )
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_active)

    def test_reactivates_when_authentik_active(self) -> None:
        change_user_is_active(self.target, False)
        with self.assertLogs("reputation_sync", "INFO"):
            self.cmd.reconcile_active_state(
                [self._row("reconcile-target@zulip.com", True)], self.admin
            )
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)

    def test_noop_when_states_match(self) -> None:
        before = self.target.is_active
        self.cmd.reconcile_active_state([self._row("reconcile-target@zulip.com", True)], self.admin)
        self.target.refresh_from_db()
        self.assertEqual(self.target.is_active, before)

    def test_skips_missing_zulip_user(self) -> None:
        # why: an Authentik user with no Zulip account must not abort the reconcile.
        self.cmd.reconcile_active_state([self._row("nobody-here@zulip.com", False)], self.admin)

    def test_skips_service_accounts(self) -> None:
        service = do_create_user(
            "portal@edenu.pl",
            "password",
            self.realm,
            "Portal Service",
            acting_user=self.admin,
        )
        self.assertTrue(service.is_active)
        self.cmd.reconcile_active_state([self._row("portal@edenu.pl", False)], self.admin)
        service.refresh_from_db()
        self.assertTrue(service.is_active)
