import logging

import requests
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from zerver.actions.user_settings import do_change_avatar_fields
from zerver.lib.avatar import is_avatar_new
from zerver.lib.upload import upload_avatar_image
from zerver.models import UserProfile
from zerver.models.custom_profile_fields import CustomProfileFieldValue


# PORTAL EDENU: Custom management command - syncs avatars from Authentik to native Zulip system
class Command(BaseCommand):
    help = "Sync user avatars from Authentik to native Zulip system (stops on first error)"

    # Authentik avatar URL configuration
    AUTHENTIK_AVATAR_URL = "https://centrum.edenu.pl/media/user-pictures/"

    # Email notification settings (only errors sent)
    ERROR_EMAIL_RECIPIENTS = ["portal@edenu.pl"]

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
        parser.add_argument("--email", type=str, default=None, help="Sync only specific user (for testing)")
        parser.add_argument("--timeout", type=int, default=30, help="HTTP request timeout in seconds")
        parser.add_argument("--no-email", action="store_true", help="Skip error email notifications")
        parser.add_argument("--verbose", action="store_true", help="Show detailed output for each user")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        target_email = options["email"]
        timeout = options["timeout"]
        verbose = options["verbose"]
        send_emails = not options["no_email"]

        self.configure_logging(verbose)

        try:
            admin_user = self.get_admin_user()
            users_to_sync = self.get_users_with_custom_avatars(target_email)

            if not users_to_sync:
                self.logger.info("No users with custom avatars found")
                return

            self.logger.info(f"Found {len(users_to_sync)} user(s) to sync")

            for user_profile in users_to_sync:
                self.sync_single_user(user_profile, timeout, admin_user, dry_run)
                self.logger.info(f"✓ Synced: {user_profile.delivery_email}")

            self.print_summary(len(users_to_sync))

        except Exception as e:
            error_msg = f"Avatar sync failed: {e!s}"
            self.logger.error(error_msg)

            if send_emails:
                self.send_error_email(error_msg)

            raise CommandError(error_msg)

    def get_users_with_custom_avatars(self, target_email: str = None) -> list[UserProfile]:
        """Query all users with 'Profilowe' field populated."""
        query = CustomProfileFieldValue.objects.filter(field__name="Profilowe").exclude(value="")

        if target_email:
            query = query.filter(user_profile__delivery_email=target_email)

        return [cpv.user_profile for cpv in query.select_related("user_profile")]

    def sync_single_user(self, user_profile, timeout, admin_user, dry_run):
        """Sync single user's avatar - raises exception on any error."""
        custom_field = CustomProfileFieldValue.objects.get(user_profile=user_profile, field__name="Profilowe")

        filename = custom_field.value
        avatar_url = f"{self.AUTHENTIK_AVATAR_URL.rstrip('/')}/{filename}"

        self.logger.info(f"Downloading: {user_profile.delivery_email} <- {avatar_url}")

        response = requests.get(avatar_url, timeout=timeout)
        response.raise_for_status()
        image_data = response.content

        if not is_avatar_new(image_data, user_profile):
            self.logger.info(f"  Unchanged - skipping: {user_profile.delivery_email}")
            return

        if dry_run:
            self.logger.info(f"  [DRY RUN] Would sync: {user_profile.delivery_email}")
            return

        file_obj = SimpleUploadedFile(
            name=filename, content=image_data, content_type=response.headers.get("Content-Type", "image/jpeg")
        )

        with transaction.atomic():
            upload_avatar_image(file_obj, user_profile, future=True)
            do_change_avatar_fields(
                user_profile, UserProfile.AVATAR_FROM_USER, skip_notify=True, acting_user=admin_user
            )

        self.logger.info("  ✓ Uploaded and updated database")

    def get_admin_user(self) -> UserProfile:
        """Get admin user for database updates."""
        admin_roles = [UserProfile.ROLE_REALM_ADMINISTRATOR, UserProfile.ROLE_REALM_OWNER]
        return UserProfile.objects.filter(
            is_active=True,
            is_bot=False,
            role__in=admin_roles,
        ).first()

    def configure_logging(self, verbose: bool = False):
        """Configure logging."""
        self.logger = logging.getLogger("avatar_sync")
        self.logger.setLevel(logging.DEBUG if verbose else logging.INFO)

        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)

    def print_summary(self, synced_count):
        """Print sync summary."""
        print("\n" + "=" * 60)
        print("Avatar Sync Summary")
        print("=" * 60)
        print(f"Total synced: {synced_count}")
        print("=" * 60)

    def send_error_email(self, error_message):
        """Send error notification email."""
        from datetime import datetime

        from django.core.mail import send_mail

        subject = "[Zulip Avatar Sync] ERROR - Avatar sync failed"
        message = f"""
Avatar sync from Authentik encountered an error.

Error Details:
{error_message}

Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

Server: centrum.edenu.pl

Please check /var/log/zulip/avatar-sync.log for details.
"""

        send_mail(subject, message, "no-reply@edenu.pl", self.ERROR_EMAIL_RECIPIENTS, fail_silently=False)
