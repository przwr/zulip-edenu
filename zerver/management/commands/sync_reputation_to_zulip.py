# PORTAL EDENU: syncs Portal Edenu reputation into Zulip custom profile fields.
import json
import logging
from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from typing_extensions import override

from zerver.actions.create_user import do_reactivate_user
from zerver.actions.custom_profile_fields import do_update_user_custom_profile_data_if_changed
from zerver.actions.users import do_deactivate_user
from zerver.lib.types import ProfileDataElementUpdateDict
from zerver.models import CustomProfileField, CustomProfileFieldValue, UserProfile

# Names match the fields the org owner creates by hand. Portal UUID is hidden
# (PORTAL_EDENU_HIDDEN_PROFILE_FIELD_NAMES); the rest are visible. Profilowe is
# the avatar filename: refreshed hourly from the snapshot so a changed picture
# re-bakes via the :07 avatar cron without waiting for the user's SAML login.
FIELD_NAMES = (
    "Reputacja",
    "Wiek",
    "Ranga",
    "Punkty Aktywności",
    "Punkty Oceny",
    "Portal UUID",
    "Profilowe",
)

# Service/non-human accounts: synced reputation would show on every client,
# so these are skipped and any stale field values are deleted.
SKIP_EMAILS = frozenset({"portal@edenu.pl"})

# Rank display names shown to users (mobile shows the raw field value).
# Green is the leaf-badge rank; RANK_GREEN is the canonical value imported by
# sync_avatars_from_authentik (single source of truth for the avatar bake trigger).
RANK_DISPLAY = {"Green": "Zielony Listek", "Normal": "Członek"}
RANK_GREEN = "Zielony Listek"


class Command(BaseCommand):
    help = "Sync Portal Edenu reputation into Zulip custom profile fields"

    ERROR_EMAIL_RECIPIENTS = ["portal@edenu.pl"]

    @override
    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--data-dir",
            default="/tmp/rep-sync",  # noqa: S108 - shared root->zulip handoff dir
            help="Directory containing snapshot.json",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Show what would be done without changes"
        )
        parser.add_argument("--email", default=None, help="Sync only a specific user (for testing)")
        parser.add_argument(
            "--no-email", action="store_true", help="Skip error email notifications"
        )
        parser.add_argument(
            "--verbose", action="store_true", help="Show detailed output for each user"
        )

    @override
    def handle(self, *args: Any, **options: Any) -> None:
        dry_run: bool = options["dry_run"]
        target_email: str | None = options["email"]
        send_emails = not options["no_email"]
        verbose: bool = options["verbose"]
        data_dir = Path(options["data_dir"])
        snapshot_path = data_dir / "snapshot.json"

        self.configure_logging(verbose)

        try:
            rows = self.load_snapshot(snapshot_path, target_email)
            if not rows:
                self.logger.info("No snapshot rows to sync")
                return

            admin_user = self.get_admin_user()
            field_by_name = self.resolve_fields(admin_user.realm)

            self.logger.info("Syncing %d user(s) from %s", len(rows), snapshot_path)

            synced = 0
            for row in rows:
                try:
                    skipped = self.sync_single_user(row, field_by_name, admin_user, dry_run)
                    if skipped:
                        self.logger.info(
                            "⊘ Non-human account — reputation cleared: %s", row["email"]
                        )
                    else:
                        synced += 1
                        self.logger.info("✓ Synced: %s", row["email"])
                except Exception as e:
                    self.logger.error("✗ Skipped %s: %s", row.get("email", "?"), e)

            if not dry_run:
                self.reconcile_active_state(rows, admin_user)
                self.export_zulip_ids(rows, admin_user, data_dir)

            self.print_summary(len(rows), synced)

        except Exception as e:
            error_msg = f"Reputation sync failed: {e!s}"
            self.logger.error(error_msg)
            if send_emails:
                self.send_error_email(error_msg)
            raise CommandError(error_msg)

    def load_snapshot(self, snapshot_path: Path, target_email: str | None) -> list[dict[str, Any]]:
        if not snapshot_path.exists():
            raise CommandError(f"Snapshot not found: {snapshot_path}")
        with snapshot_path.open() as f:
            data = json.load(f)
        users = data.get("users", [])
        if target_email:
            users = [u for u in users if u.get("email") == target_email]
        return users

    def resolve_fields(self, realm: Any) -> dict[str, CustomProfileField]:
        present = CustomProfileField.objects.filter(realm=realm, name__in=FIELD_NAMES)
        field_by_name = {f.name: f for f in present}
        missing = [n for n in FIELD_NAMES if n not in field_by_name]
        if missing:
            self.logger.warning(
                "Missing custom field(s) (create them by hand in Zulip): %s", ", ".join(missing)
            )
        return field_by_name

    def sync_single_user(
        self,
        row: dict[str, Any],
        field_by_name: dict[str, CustomProfileField],
        admin_user: UserProfile,
        dry_run: bool,
    ) -> bool:
        email = row["email"]
        user_profile = UserProfile.objects.filter(
            delivery_email__iexact=email.strip(), realm=admin_user.realm
        ).first()
        if user_profile is None:
            raise ValueError(f"no Zulip user with delivery_email={email}")

        # Service/non-human accounts show no reputation anywhere.
        if email.strip().lower() in SKIP_EMAILS:
            self.clear_reputation_fields(user_profile, field_by_name, dry_run)
            return True

        data = self.build_field_values(row, field_by_name)
        if not data:
            raise ValueError("no writable fields (create the custom fields in Zulip first)")

        if dry_run:
            self.logger.info("  [DRY RUN] %s -> %s", email, data)
            return False

        do_update_user_custom_profile_data_if_changed(
            user_profile, data, acting_user=admin_user, notify=False
        )
        return False

    def clear_reputation_fields(
        self,
        user_profile: UserProfile,
        field_by_name: dict[str, CustomProfileField],
        dry_run: bool,
    ) -> None:
        """Delete any reputation field values so non-human accounts show nothing."""
        field_ids = [f.id for f in field_by_name.values()]
        if not field_ids:
            return
        if dry_run:
            self.logger.info("  [DRY RUN] clear reputation for %s", user_profile.delivery_email)
            return
        CustomProfileFieldValue.objects.filter(
            user_profile=user_profile, field_id__in=field_ids
        ).delete()

    def build_field_values(
        self, row: dict[str, Any], field_by_name: dict[str, CustomProfileField]
    ) -> list[ProfileDataElementUpdateDict]:
        total = int(row.get("total_points", 0))
        raw = {
            "Reputacja": self.reputation_field_value(total),
            "Wiek": str(row.get("wiek", "")),
            "Ranga": RANK_DISPLAY.get(str(row.get("rank") or "Normal"), str(row.get("rank"))),
            "Punkty Aktywności": str(int(row.get("activity_points", 0))),
            "Punkty Oceny": str(int(row.get("rating_points", 0))),
            "Portal UUID": str(row["portal_uuid"]),
        }
        # PORTAL EDENU: write Profilowe only when the snapshot carries a picture —
        # never blank it. An empty write would drop the (stale) filename for an
        # anonymized user, and the avatar cron's scrub path (source file gone ->
        # scrub) keys off that stale value.
        picture = str(row.get("picture") or "")
        if picture:
            raw["Profilowe"] = picture
        return [
            {"id": field_by_name[name].id, "value": value}
            for name, value in raw.items()
            if name in field_by_name
        ]

    @staticmethod
    def reputation_field_value(total_points: int) -> str:
        # Must match computeReputationBar() in portal-edenu/front/src/lib/reputation.ts.
        display_total = max(0, min(total_points, 100))
        suffix = "+" if total_points > 100 else ""
        return f"{display_total}{suffix}%"

    def get_admin_user(self) -> UserProfile:
        admin = UserProfile.objects.filter(
            is_active=True,
            is_bot=False,
            role__in=[UserProfile.ROLE_REALM_ADMINISTRATOR, UserProfile.ROLE_REALM_OWNER],
        ).first()
        if admin is None:
            raise CommandError("No admin user found")
        return admin

    def configure_logging(self, verbose: bool = False) -> None:
        self.logger = logging.getLogger("reputation_sync")
        self.logger.setLevel(logging.DEBUG if verbose else logging.INFO)
        # Stop propagation to Django's root logger: a routine per-user skip logs
        # at error level here, and root has mail_admins attached, so without
        # this every "no Zulip user" skip (e.g. Authentik's root@example.com)
        # spams the admins. Whole-sync failures still email via send_error_email
        # below (send_mail, not logging). Mirrors sync_avatars_from_authentik.
        self.logger.propagate = False
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
            self.logger.addHandler(handler)

    def print_summary(self, total: int, synced: int) -> None:
        print("\n" + "=" * 60)
        print("Reputation Sync Summary")
        print("=" * 60)
        print(f"Total rows: {total}")
        print(f"Synced:     {synced}")
        print(f"Skipped:    {total - synced}")
        print("=" * 60)

    def reconcile_active_state(self, rows: list[dict[str, Any]], admin_user: UserProfile) -> None:
        # PORTAL EDENU: suspend safety net. Authentik is_active is the source of truth;
        # if a Zulip account diverges (live suspend call failed, or a direct Authentik
        # suspension bypassed the panel), bring Zulip into line. Idempotent: no-op when
        # states already match. Deactivation revokes Zulip sessions + API tokens.
        deactivated = 0
        reactivated = 0
        for row in rows:
            email = str(row["email"]).strip()
            if email.lower() in SKIP_EMAILS:
                continue
            target = UserProfile.objects.filter(
                delivery_email__iexact=email, realm=admin_user.realm
            ).first()
            if target is None:
                continue
            authentik_active = bool(row.get("is_active", True))
            if target.is_active == authentik_active:
                continue
            if authentik_active:
                do_reactivate_user(target, acting_user=admin_user)
                reactivated += 1
                self.logger.info("↻ Reactivated (Authentik active): %s", email)
            else:
                do_deactivate_user(target, acting_user=admin_user)
                deactivated += 1
                self.logger.info("⊗ Deactivated (Authentik inactive): %s", email)
        if deactivated or reactivated:
            self.logger.info(
                "Suspension reconciliation: %d deactivated, %d reactivated",
                deactivated,
                reactivated,
            )

    def export_zulip_ids(
        self, rows: list[dict[str, Any]], admin_user: UserProfile, data_dir: Path
    ) -> None:
        # why: portal-edenu links reputation pages to #narrow/dm/{id}; the numeric
        # Zulip id is not PII (unlike email) and is immutable for a user's life.
        wanted = {str(r["email"]).strip().lower() for r in rows}
        qs = UserProfile.objects.filter(realm=admin_user.realm, is_active=True).values_list(
            "delivery_email", "id"
        )
        out = {email: uid for email, uid in qs if email.strip().lower() in wanted}
        (data_dir / "zulip_ids.json").write_text(json.dumps(out))

    def send_error_email(self, error_message: str) -> None:
        from django.core.mail import send_mail

        send_mail(
            "[Zulip Reputation Sync] ERROR",
            (
                f"Reputation sync failed.\n\nError:\n{error_message}\n\n"
                f"Time: {datetime.now(tz=timezone.utc):%Y-%m-%d %H:%M:%S} UTC\n"
                "Server: centrum.edenu.pl\n\n"
                "Check /var/log/zulip/reputation-sync.log"
            ),
            "no-reply@edenu.pl",
            self.ERROR_EMAIL_RECIPIENTS,
            fail_silently=False,
        )
