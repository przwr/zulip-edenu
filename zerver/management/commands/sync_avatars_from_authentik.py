import logging
from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from typing_extensions import override

from zerver.actions.user_settings import do_change_avatar_fields, do_scrub_avatar_images
from zerver.lib.avatar import is_avatar_new
from zerver.lib.avatar_hash import user_avatar_content_hash
from zerver.lib.mime_types import guess_type
from zerver.lib.upload import delete_avatar_image, upload_avatar_image
from zerver.models import UserProfile
from zerver.models.custom_profile_fields import CustomProfileFieldValue

# Module logger. Level set to INFO at import so the login-hook path (which
# never calls configure_logging, runs in the worker process) passes its INFO
# records up to Django's root logger -> server.log. The cron path re-levels
# (DEBUG when --verbose) in configure_logging and sets propagate=False there.
_log = logging.getLogger("avatar_sync")
_log.setLevel(logging.INFO)

# Authentik media dir on the host; Zulip and Authentik share the box, so the
# avatar command reads the file directly instead of HTTPS-fetching the public
# URL. 'zulip' must be able to traverse+read this dir (verified on prod).
AUTHENTIK_MEDIA_DIR = Path("/opt/portal-edenu/authentik/data/media/user-pictures")

# Rank badges, rasterized once from portal-edenu's SVGs and shipped as sibling
# PNGs: this server's libvips build has no SVG loader, but PNG loads fine.
# Map: Polish rank display value (as written by sync_reputation_to_zulip) ->
# badge PNG. Add a row here when a new rank goes live; None rank = no badge.
RANK_BADGES = {
    "Zielony Listek": Path(__file__).with_name("green_leaf_badge.png"),
    "Prowadzący": Path(__file__).with_name("lead_badge.png"),
}

# Badge placement on the square avatar (ratios of side length).
# Badge fills the bottom-left corner of the square: size 0.36 (1.5× original),
# left 0.0, top 0.64 (= 1.0 - size) so it sits flush with the left and bottom
# edges. CAVEAT: avatars render as border-radius:50% (a circle), so the disc's
# bottom-left rim — which reaches the corner, outside the inscribed circle —
# is clipped by the avatar's curve. The badge reads as tucked into the corner;
# its outer arc follows the circle. If more must stay visible, reduce the size
# or pull the centre inward (toward 0.5, 0.5).
_BADGE_SIZE_RATIO = 0.36
_BADGE_LEFT_RATIO = 0.0
_BADGE_TOP_RATIO = 0.64


def bake_badge(image_data: bytes, badge_png_path: Path) -> bytes | None:
    """Composite the badge PNG onto the avatar; return PNG bytes, or None if
    libvips is unavailable (caller keeps the plain avatar). Never raises."""
    try:
        import pyvips
    except ImportError:
        _log.warning("pyvips not installed; cannot bake badge")
        return None

    try:
        base = pyvips.Image.new_from_buffer(image_data, "")
    except Exception as e:
        _log.warning("bake_badge: cannot load base avatar (%s: %s)", type(e).__name__, e)
        return None

    try:
        badge = pyvips.Image.new_from_buffer(badge_png_path.read_bytes(), "")
    except Exception as e:
        _log.warning(
            "bake_badge: cannot load badge PNG %s (%s: %s)",
            badge_png_path.name,
            type(e).__name__,
            e,
        )
        return None

    try:
        side = min(base.width, base.height)
        # Centre-crop to square so the badge survives Zulip's own CENTRE-crop resize.
        base = base.crop((base.width - side) // 2, (base.height - side) // 2, side, side)
        if base.bands < 4:
            base = base.addalpha()
        # Scale the 128px badge to the target size (version-independent vs scale=).
        badge = badge.resize(_BADGE_SIZE_RATIO * side / 128.0)
        if badge.bands < 4:
            badge = badge.addalpha()
        out = base.composite2(
            badge,
            "over",
            x=round(_BADGE_LEFT_RATIO * side),
            y=round(_BADGE_TOP_RATIO * side),
        )
        return out.write_to_buffer(".png")
    except Exception as e:
        _log.warning("bake_badge: composite/encode failed (%s: %s)", type(e).__name__, e)
        return None


def get_admin_user() -> UserProfile:
    """First active owner/admin; used as acting_user for avatar field changes."""
    admin_roles = [UserProfile.ROLE_REALM_ADMINISTRATOR, UserProfile.ROLE_REALM_OWNER]
    admin = UserProfile.objects.filter(
        is_active=True,
        is_bot=False,
        role__in=admin_roles,
    ).first()
    if admin is None:
        raise CommandError("No admin user found")
    return admin


def get_ranks_for_users(users: list[UserProfile]) -> dict[int, str]:
    """Map user_profile_id -> Polish rank string (missing -> "Normal")."""
    rows = CustomProfileFieldValue.objects.filter(
        field__name="Ranga", user_profile__in=users
    ).values_list("user_profile_id", "value")
    return {uid: (value or "Normal") for uid, value in rows}


def sync_user_avatar(
    user_profile: UserProfile,
    *,
    admin_user: UserProfile,
    rank: str = "Normal",
    dry_run: bool = False,
    logger: logging.Logger | None = None,
) -> None:
    """Read the user's Authentik avatar off disk, bake the rank badge if any, and
    upload it to Zulip when the baked bytes differ. Raises on unexpected errors
    (callers wrap in try/except). Shared by the cron command and the login hook."""
    log = logger or _log

    custom_field = CustomProfileFieldValue.objects.get(
        user_profile=user_profile, field__name="Profilowe"
    )

    filename = custom_field.value
    avatar_path = (AUTHENTIK_MEDIA_DIR / filename).resolve()
    # Trust-boundary guard: Profilowe is a user-editable field, so a malicious
    # value like "../../etc/secret" must not read outside the media dir. Caught
    # by both callers' per-user guards -> logged skip, never an abort.
    if not avatar_path.is_relative_to(AUTHENTIK_MEDIA_DIR.resolve()):
        # PORTAL EDENU: path-escape or missing source file means the Authentik
        # picture is gone (e.g. user was anonymized in portal-edenu, which
        # deletes the file from disk). Scrub the Zulip avatar so the real face
        # doesn't linger in storage. Anonymized users are deactivated and can't
        # log in, so the login-hook self-heal never fires — the cron is the only
        # path that reaches them. Gate on AVATAR_FROM_USER so we don't touch a
        # user who already has a default/jdenticon avatar.
        if user_profile.avatar_source == UserProfile.AVATAR_FROM_USER:
            _scrub_and_return(user_profile, admin_user, dry_run, log)
        else:
            log.info(
                "  Source missing but avatar already default — skipping: %s",
                user_profile.delivery_email,
            )
        return
    log.info("Reading: %s <- %s", user_profile.delivery_email, avatar_path)

    try:
        # Fails fast (FileNotFoundError/PermissionError) if Authentik swapped the
        # filename; callers isolate per-user so this becomes a skip, not an abort.
        content = avatar_path.read_bytes()
    except (FileNotFoundError, PermissionError):
        # PORTAL EDENU: same scrub path as the escape guard above — the Authentik
        # source file was deleted (anonymization) or is unreadable.
        if user_profile.avatar_source == UserProfile.AVATAR_FROM_USER:
            _scrub_and_return(user_profile, admin_user, dry_run, log)
        else:
            log.info(
                "  Source missing but avatar already default — skipping: %s",
                user_profile.delivery_email,
            )
        return
    name = filename
    content_type = guess_type(filename)[0] or "image/jpeg"

    badge_png = RANK_BADGES.get(rank)
    if badge_png is not None:
        baked = bake_badge(content, badge_png)
        if baked is not None:
            content = baked
            name = filename.rsplit(".", 1)[0] + ".png"
            content_type = "image/png"
        else:
            log.warning(
                "  Rank %s but bake unavailable; using plain avatar: %s",
                rank,
                user_profile.delivery_email,
            )

    # Dedup by the (baked) content hash. New users (avatar_hash=None) and any
    # real change (new pic, rank change) upload; identical baked bytes skip.
    if not is_avatar_new(content, user_profile):
        log.info("  Unchanged - skipping: %s", user_profile.delivery_email)
        return

    if dry_run:
        log.info("  [DRY RUN] Would sync: %s", user_profile.delivery_email)
        return

    file_obj = SimpleUploadedFile(name=name, content=content, content_type=content_type)

    with transaction.atomic(savepoint=False):
        old_version = user_profile.avatar_version
        upload_avatar_image(file_obj, user_profile, future=True)
        do_change_avatar_fields(
            user_profile,
            UserProfile.AVATAR_FROM_USER,
            skip_notify=True,
            acting_user=admin_user,
        )
        # Zulip versions every avatar under a versioned path and never GCs the
        # old ones — the cron re-sync would otherwise leave one orphaned
        # version per run per user (this is what filled uploads/avatars).
        # Mirrors do_scrub_avatar_images; delete_local_file no-ops missing files.
        for version in range(1, old_version + 1):
            delete_avatar_image(user_profile, version)
        # Persist hash so next run's is_avatar_new() skips — mirrors LDAP path.
        user_profile.avatar_hash = user_avatar_content_hash(content)
        user_profile.save(update_fields=["avatar_hash"])

    log.info("  ✓ Uploaded and updated database")


def _scrub_and_return(
    user_profile: UserProfile,
    admin_user: UserProfile,
    dry_run: bool,
    log: logging.Logger,
) -> None:
    """PORTAL EDENU: scrub the Zulip avatar when the Authentik source file is gone.
    Called when the source is missing or escapes the media dir (anonymized user).
    Resets the avatar to the realm default and deletes all uploaded versions."""
    if dry_run:
        log.info("  [DRY RUN] Would scrub avatar: %s", user_profile.delivery_email)
        return
    with transaction.atomic(savepoint=False):
        do_scrub_avatar_images(user_profile, acting_user=admin_user)
    log.info("  ✓ Source missing — scrubbed Zulip avatar: %s", user_profile.delivery_email)


# PORTAL EDENU: syncs avatars from Authentik to native Zulip system; bakes the
# rank badge onto the avatar before upload when Ranga is a ranked value.
class Command(BaseCommand):
    help = "Sync user avatars from Authentik to native Zulip system (logs and continues on per-user errors)"

    # Email notification settings (only errors sent)
    ERROR_EMAIL_RECIPIENTS = ["portal@edenu.pl"]

    @override
    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--dry-run", action="store_true", help="Show what would be done without making changes"
        )
        parser.add_argument("--email", default=None, help="Sync only specific user (for testing)")
        parser.add_argument(
            "--no-email", action="store_true", help="Skip error email notifications"
        )
        parser.add_argument(
            "--verbose", action="store_true", help="Show detailed output for each user"
        )
        parser.add_argument(
            "--cleanup-only",
            action="store_true",
            help="Delete orphaned old avatar versions for all synced users "
            "(reclaims disk without re-downloading/re-uploading). Keeps the current version.",
        )

    @override
    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = options["dry_run"]
        target_email = options["email"]
        verbose: bool = options["verbose"]
        send_emails = not options["no_email"]
        cleanup_only: bool = options["cleanup_only"]

        self.configure_logging(verbose)

        try:
            admin_user = get_admin_user()
            users_to_sync = self.get_users_with_custom_avatars(target_email)

            if not users_to_sync:
                _log.info("No users with custom avatars found")
                return

            # PORTAL EDENU: GC orphaned old avatar versions. Zulip never deletes
            # previous versions on re-upload, so the cron re-sync filled the disk.
            if cleanup_only:
                _log.info("Found %d user(s) for cleanup", len(users_to_sync))
                total_deleted = 0
                for user_profile in users_to_sync:
                    total_deleted += self.cleanup_single_user(user_profile, dry_run)
                    _log.info("✓ Cleaned: %s", user_profile.delivery_email)
                _log.info(
                    "Cleaned %d old avatar version(s) across %d user(s)",
                    total_deleted,
                    len(users_to_sync),
                )
                return

            _log.info("Found %d user(s) to sync", len(users_to_sync))
            rank_by_user = get_ranks_for_users(users_to_sync)

            for user_profile in users_to_sync:
                rank = rank_by_user.get(user_profile.id, "Normal")
                # Per-user isolation: a stale/404 Profilowe (Authentik swapped the
                # filename out from under us) must not abort the whole batch — log
                # and continue. The sync_user_avatar call now scrubs the Zulip
                # avatar when the source file is gone (anonymization), so a missing
                # file no longer leaves a stale avatar. Matches the documented
                # "sync what's possible, log mismatches, never abort".
                try:
                    sync_user_avatar(
                        user_profile,
                        admin_user=admin_user,
                        rank=rank,
                        dry_run=dry_run,
                    )
                    _log.info("✓ Synced: %s", user_profile.delivery_email)
                except Exception as e:
                    _log.warning(
                        "✗ Skipped %s: %s: %s",
                        user_profile.delivery_email,
                        type(e).__name__,
                        e,
                    )

            self.print_summary(len(users_to_sync))

        except Exception as e:
            error_msg = f"Avatar sync failed: {e!s}"
            _log.error(error_msg)

            if send_emails:
                self.send_error_email(error_msg)

            raise CommandError(error_msg)

    def cleanup_single_user(self, user_profile: UserProfile, dry_run: bool) -> int:
        """Delete orphaned old avatar versions, keeping only the current one.

        Same helper (delete_avatar_image) the sync path and do_scrub_avatar_images
        use; the local backend no-ops on missing files, so version gaps are safe.
        Returns the number of versions (that would be) removed."""
        deleted = 0
        for version in range(1, user_profile.avatar_version):
            if dry_run:
                _log.info(
                    "  [DRY RUN] Would delete avatar version %d: %s",
                    version,
                    user_profile.delivery_email,
                )
            else:
                delete_avatar_image(user_profile, version)
            deleted += 1
        return deleted

    def get_users_with_custom_avatars(self, target_email: str | None = None) -> list[UserProfile]:
        """Query all users with 'Profilowe' field populated."""
        query = CustomProfileFieldValue.objects.filter(field__name="Profilowe").exclude(value="")

        if target_email:
            query = query.filter(user_profile__delivery_email=target_email)

        return [cpv.user_profile for cpv in query.select_related("user_profile")]

    def configure_logging(self, verbose: bool = False) -> None:
        """Cron-only logging setup. Attach a StreamHandler once and stop
        propagation: the cron's stdout/stderr is redirected to avatar-sync.log,
        so the StreamHandler is the single emitter (Django's root handler would
        otherwise double every line). The login hook runs in a different process
        that never calls this, so it keeps propagate=True (default) and its INFO
        reaches server.log via Django's root logger."""
        _log.setLevel(logging.DEBUG if verbose else logging.INFO)
        _log.propagate = False
        if not any(isinstance(h, logging.StreamHandler) for h in _log.handlers):
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
            _log.addHandler(handler)
        self.logger = _log

    def print_summary(self, synced_count: int) -> None:
        """Print sync summary."""
        print("\n" + "=" * 60)
        print("Avatar Sync Summary")
        print("=" * 60)
        print(f"Total synced: {synced_count}")
        print("=" * 60)

    def send_error_email(self, error_message: str) -> None:
        """Send error notification email."""
        from django.core.mail import send_mail

        subject = "[Zulip Avatar Sync] ERROR - Avatar sync failed"
        message = f"""
Avatar sync from Authentik encountered an error.

Error Details:
{error_message}

Time: {datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}

Server: centrum.edenu.pl

Please check /var/log/zulip/avatar-sync.log for details.
"""

        send_mail(
            subject, message, "no-reply@edenu.pl", self.ERROR_EMAIL_RECIPIENTS, fail_silently=False
        )
