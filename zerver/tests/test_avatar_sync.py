# PORTAL EDENU: tests for the Authentik→Zulip avatar sync command,
# specifically the scrub-on-missing-source-file path (anonymization).
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from zerver.actions.custom_profile_fields import try_add_realm_custom_profile_field
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.test_helpers import avatar_disk_path, get_test_image_file
from zerver.management.commands.sync_avatars_from_authentik import get_admin_user, sync_user_avatar
from zerver.models import CustomProfileField, CustomProfileFieldValue, UserProfile
from zerver.models.realms import get_realm


class AvatarSyncScrubTest(ZulipTestCase):
    """PORTAL EDENU: when the Authentik source avatar file is deleted (user
    anonymized in portal-edenu), the cron must scrub the Zulip avatar — the
    deactivated user can never log in, so the login-hook self-heal never fires.
    """

    def _set_up_user_with_avatar(self) -> UserProfile:
        """Upload a real avatar, create a Profilowe field pointing at a
        non-existent Authentik file, and return the user."""
        user = self.example_user("hamlet")
        self.login("hamlet")
        with get_test_image_file("img.png") as image_file:
            self.client_post("/json/users/me/avatar", {"file": image_file})

        user.refresh_from_db()
        assert user.avatar_source == UserProfile.AVATAR_FROM_USER

        realm = get_realm("zulip")
        field = try_add_realm_custom_profile_field(
            realm, "Profilowe", CustomProfileField.SHORT_TEXT
        )
        # Point at a filename that will NOT exist in the patched media dir.
        CustomProfileFieldValue.objects.update_or_create(
            user_profile=user,
            field=field,
            defaults={"value": "men/deleted_avatar.jpg"},
        )
        return user

    def test_scrub_avatar_when_source_file_missing(self) -> None:
        """Source file gone (anonymized user) → avatar scrubbed to default."""
        user = self._set_up_user_with_avatar()

        avatar_path = avatar_disk_path(user)
        avatar_original = avatar_disk_path(user, original=True)
        avatar_medium = avatar_disk_path(user, medium=True)
        assert os.path.isfile(avatar_path)

        admin = get_admin_user()

        # Patch the media dir to an empty temp dir — the Profilowe filename
        # doesn't exist there, simulating a deleted Authentik source file.
        # assertLogs captures the sync command's INFO output so it doesn't trip
        # Zulip's --ban-console-output check.
        with (
            TemporaryDirectory() as tmpdir,
            patch(
                "zerver.management.commands.sync_avatars_from_authentik.AUTHENTIK_MEDIA_DIR",
                Path(tmpdir),
            ),
            self.assertLogs("avatar_sync", level="INFO"),
        ):
            sync_user_avatar(user, admin_user=admin, rank="Normal")

        user.refresh_from_db()
        # do_scrub_avatar_images resets to the realm default (jdenticon).
        self.assertNotEqual(user.avatar_source, UserProfile.AVATAR_FROM_USER)
        # All uploaded avatar versions deleted from disk.
        assert not os.path.isfile(avatar_path)
        assert not os.path.isfile(avatar_original)
        assert not os.path.isfile(avatar_medium)

    def test_no_scrub_when_avatar_already_default(self) -> None:
        """User with a default avatar (not AVATAR_FROM_USER) → no scrub needed."""
        user = self._set_up_user_with_avatar()
        # Force avatar_source to default so the guard skips scrubbing.
        user.avatar_source = UserProfile.AVATAR_FROM_JDENTICON
        user.save(update_fields=["avatar_source"])

        avatar_path = avatar_disk_path(user)
        admin = get_admin_user()

        with (
            TemporaryDirectory() as tmpdir,
            patch(
                "zerver.management.commands.sync_avatars_from_authentik.AUTHENTIK_MEDIA_DIR",
                Path(tmpdir),
            ),
            self.assertLogs("avatar_sync", level="INFO"),
        ):
            sync_user_avatar(user, admin_user=admin, rank="Normal")

        # Avatar files still on disk — we didn't scrub.
        assert os.path.isfile(avatar_path)

    def test_dry_run_does_not_scrub(self) -> None:
        """Dry run should never mutate — source missing but no scrub happens."""
        user = self._set_up_user_with_avatar()

        avatar_path = avatar_disk_path(user)
        admin = get_admin_user()

        with (
            TemporaryDirectory() as tmpdir,
            patch(
                "zerver.management.commands.sync_avatars_from_authentik.AUTHENTIK_MEDIA_DIR",
                Path(tmpdir),
            ),
            self.assertLogs("avatar_sync", level="INFO"),
        ):
            sync_user_avatar(user, admin_user=admin, rank="Normal", dry_run=True)

        user.refresh_from_db()
        # Dry run — avatar_source unchanged, files still present.
        self.assertEqual(user.avatar_source, UserProfile.AVATAR_FROM_USER)
        assert os.path.isfile(avatar_path)
