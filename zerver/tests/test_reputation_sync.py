# PORTAL EDENU: tests for the Profilowe (avatar filename) refresh in
# sync_reputation_to_zulip — keeps Zulip's Profilowe fresh hourly so the :07
# avatar cron re-bakes a changed picture without waiting for a SAML login.
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

    def _row(self, picture: str = "") -> dict[str, Any]:
        return {
            "email": "profilowe@zulip.com",
            "portal_uuid": "uuid-1",
            "rank": "Normal",
            "total_points": 0,
            "activity_points": 0,
            "rating_points": 0,
            "wiek": "",
            "picture": picture,
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
