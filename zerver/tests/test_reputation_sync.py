# PORTAL EDENU: tests for the Profilowe (avatar filename) and display_name
# (full_name) refresh in sync_reputation_to_zulip — keeps Zulip fresh hourly so
# a changed picture re-bakes via the :07 avatar cron and a changed display name
# lands without waiting for the user's next SAML login.
from typing import Any

from typing_extensions import override

from zerver.actions.create_user import do_create_user
from zerver.lib.test_classes import ZulipTestCase
from zerver.management.commands.sync_reputation_to_zulip import Command
from zerver.models import CustomProfileField, CustomProfileFieldValue
from zerver.models.realms import get_realm


class ProfiloweSyncTest(ZulipTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.realm = get_realm("zulip")  # why: the default test realm seeded by ZulipTestCase
        self.admin = self.example_user("iago")
        self.profilowe_field = CustomProfileField.objects.create(
            realm=self.realm,
            name="Profilowe",
            field_type=CustomProfileField.SHORT_TEXT,
        )
        self.cmd = Command()
        self.cmd.configure_logging(verbose=False)

    def _field_by_name(self) -> dict[str, CustomProfileField]:
        return {"Profilowe": self.profilowe_field}

    def _row(
        self, picture: str = "", display_name: str = "", email: str = "profilowe@zulip.com"
    ) -> dict[str, Any]:
        return {
            "email": email,
            "portal_uuid": "uuid-1",
            "rank": "Normal",
            "total_points": 0,
            "activity_points": 0,
            "rating_points": 0,
            "wiek": "",
            "picture": picture,
            "display_name": display_name,
            "is_active": True,
        }

    def test_includes_profilowe_when_picture_present(self) -> None:
        data = self.cmd.build_field_values(self._row(picture="abc.jpeg"), self._field_by_name())
        values = {d["id"]: d["value"] for d in data}
        self.assertEqual(values[self.profilowe_field.id], "abc.jpeg")

    def test_omits_profilowe_when_picture_absent(self) -> None:
        # why: never blank — anonymized users must keep the stale filename so the
        # avatar cron's scrub path (source file gone -> scrub) still fires.
        for row in (self._row(picture=""), self._row()):
            data = self.cmd.build_field_values(row, self._field_by_name())
            self.assertNotIn(self.profilowe_field.id, {d["id"] for d in data})

    def test_tolerates_missing_profilowe_field(self) -> None:
        # why: rollout — the field may not exist yet in an org; skip, don't crash.
        data = self.cmd.build_field_values(self._row(picture="abc.jpeg"), {})
        self.assertEqual(data, [])

    def test_sync_single_user_writes_profilowe(self) -> None:
        user = do_create_user(
            "profilowe@zulip.com",
            "password",
            self.realm,
            "Profilowe Target",
            acting_user=self.admin,
        )
        self.cmd.sync_single_user(
            self._row(picture="new-crop.jpeg"), self._field_by_name(), self.admin, False
        )
        value = CustomProfileFieldValue.objects.get(user_profile=user, field=self.profilowe_field)
        self.assertEqual(value.value, "new-crop.jpeg")

    def test_sync_single_user_updates_full_name(self) -> None:
        user = do_create_user(
            "displayname@zulip.com",
            "password",
            self.realm,
            "Old Name",
            acting_user=self.admin,
        )
        self.cmd.sync_single_user(
            self._row(
                picture="x.jpeg",
                display_name="  Janek (Kolec) Kowalsky ",
                email="displayname@zulip.com",
            ),
            self._field_by_name(),
            self.admin,
            False,
        )
        user.refresh_from_db()
        self.assertEqual(user.full_name, "Janek (Kolec) Kowalsky")

    def test_sync_single_user_skips_unchanged_or_absent_name(self) -> None:
        user = do_create_user(
            "same-name@zulip.com",
            "password",
            self.realm,
            "Same Name",
            acting_user=self.admin,
        )
        for row in (
            self._row(picture="x.jpeg", display_name="Same Name", email="same-name@zulip.com"),
            self._row(picture="x.jpeg", email="same-name@zulip.com"),
        ):
            self.cmd.sync_single_user(row, self._field_by_name(), self.admin, False)
            user.refresh_from_db()
            self.assertEqual(user.full_name, "Same Name")

    def test_sync_single_user_keeps_old_name_on_invalid(self) -> None:
        # why: names ending in |NN are ambiguous for mention markup — must be
        # rejected and skipped, not crash the hourly batch.
        user = do_create_user(
            "badname@zulip.com",
            "password",
            self.realm,
            "Good Name",
            acting_user=self.admin,
        )
        self.cmd.sync_single_user(
            self._row(picture="x.jpeg", display_name="Bad Name|15", email="badname@zulip.com"),
            self._field_by_name(),
            self.admin,
            False,
        )
        user.refresh_from_db()
        self.assertEqual(user.full_name, "Good Name")

    def test_wspierajacy_written_from_snapshot_flag(self) -> None:
        # PORTAL EDENU: the snapshot's is_supporter drives the Wspierajacy
        # field the avatar cron reads for the bottom-right badge bake.
        field = CustomProfileField.objects.create(
            realm=self.realm,
            name="Wspierający",
            field_type=CustomProfileField.SHORT_TEXT,
        )
        fields = {**self._field_by_name(), "Wspierający": field}

        data = self.cmd.build_field_values({**self._row(), "is_supporter": True}, fields)
        values = {d["id"]: d["value"] for d in data}
        self.assertEqual(values[field.id], "Tak")

    def test_wspierajacy_cleared_when_not_supporter(self) -> None:
        # why: always write Tak/blank — a lapsed supporter must lose the badge.
        field = CustomProfileField.objects.create(
            realm=self.realm,
            name="Wspierający",
            field_type=CustomProfileField.SHORT_TEXT,
        )
        fields = {**self._field_by_name(), "Wspierający": field}

        data = self.cmd.build_field_values(self._row(), fields)
        values = {d["id"]: d["value"] for d in data}
        self.assertEqual(values[field.id], "")
