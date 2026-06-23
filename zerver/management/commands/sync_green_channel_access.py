# PORTAL EDENU: keeps Green-badge (Zielony Listek) users out of private channels
# marked with can_subscribe_group = CZLONKOWIE_GROUP_NAME, while auto-subscribing
# every non-Green member. Mirrors sync_reputation_to_zulip (same shape, logging,
# per-user never-abort). The Ranga custom field is the single source of truth for
# who is Green — already written hourly by the reputation sync at :05.
import logging
from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from typing_extensions import override

from zerver.actions.streams import bulk_add_subscriptions, bulk_remove_subscriptions
from zerver.actions.user_groups import (
    bulk_add_members_to_user_groups,
    bulk_remove_members_from_user_groups,
)
from zerver.models import NamedUserGroup, Stream, Subscription, UserGroupMembership, UserProfile
from zerver.models.custom_profile_fields import CustomProfileFieldValue

# Polish rank value written by sync_reputation_to_zulip. Any other value (or a
# missing field) means non-Green.
RANK_GREEN = "Zielony Listek"

# The marker: channels whose "Who can subscribe" (can_subscribe_group) is this
# group are Green-restricted. Must be private (invite_only) — only on a private
# channel does removing a subscription fully block reads. The group is also the
# vehicle that grants non-Green members read access without an explicit
# subscription (membership in can_subscribe_group grants content access).
CZLONKOWIE_GROUP_NAME = "Członkowie"

_log = logging.getLogger("green_channel_sync")
_log.setLevel(logging.INFO)


class Command(BaseCommand):
    help = "Keep Green-badge (Zielony Listek) users out of marked private channels"

    @override
    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Show changes without applying")
        parser.add_argument("--verbose", action="store_true", help="Detailed output")

    @override
    def handle(self, *args: Any, **options: Any) -> None:
        dry_run: bool = options["dry_run"]
        verbose: bool = options["verbose"]
        self.configure_logging(verbose)

        admin_user = self.get_admin_user()
        realm = admin_user.realm

        group = self.get_marker_group(realm)
        if group is None:
            self.logger.info("Group '%s' not found — nothing to sync", CZLONKOWIE_GROUP_NAME)
            return

        green_ids, non_green_ids = self.partition_users(realm)
        self.logger.info(
            "Green: %d, non-Green: %d (total active humans)", len(green_ids), len(non_green_ids)
        )

        if dry_run:
            self.logger.info("[DRY RUN] no changes will be applied")

        self.reconcile_group(group, green_ids, non_green_ids, admin_user, dry_run)
        self.reconcile_channels(realm, group, green_ids, non_green_ids, admin_user, dry_run)

    # -- data gathering -----------------------------------------------------

    def get_marker_group(self, realm: Any) -> NamedUserGroup | None:
        return NamedUserGroup.objects.filter(
            realm_for_sharding=realm, name=CZLONKOWIE_GROUP_NAME, is_system_group=False
        ).first()

    def partition_users(self, realm: Any) -> tuple[set[int], set[int]]:
        green_by_field = set(
            CustomProfileFieldValue.objects.filter(
                field__name="Ranga", value=RANK_GREEN, user_profile__realm=realm
            ).values_list("user_profile_id", flat=True)
        )
        active_humans = set(
            UserProfile.objects.filter(realm=realm, is_active=True, is_bot=False).values_list(
                "id", flat=True
            )
        )
        green = green_by_field & active_humans
        non_green = active_humans - green
        return green, non_green

    # -- reconciliation -----------------------------------------------------

    def reconcile_group(
        self,
        group: NamedUserGroup,
        green_ids: set[int],
        non_green_ids: set[int],
        admin_user: UserProfile,
        dry_run: bool,
    ) -> None:
        # Group must equal the non-Green set: it gates read access on marked
        # channels, so a Green member would bypass the subscription removal.
        current = set(group.direct_members.values_list("id", flat=True))
        to_remove = (current & green_ids) - non_green_ids
        to_add = non_green_ids - current
        if not to_remove and not to_add:
            self.logger.info("Group '%s' already correct (%d members)", group.name, len(current))
            return
        self.logger.info(
            "Group '%s': +%d -%d (target %d)",
            group.name,
            len(to_add),
            len(to_remove),
            len(non_green_ids),
        )
        if dry_run:
            return
        if to_remove:
            bulk_remove_members_from_user_groups([group], list(to_remove), acting_user=admin_user)
        if to_add:
            bulk_add_members_to_user_groups([group], list(to_add), acting_user=admin_user)

    def reconcile_channels(
        self,
        realm: Any,
        group: NamedUserGroup,
        green_ids: set[int],
        non_green_ids: set[int],
        admin_user: UserProfile,
        dry_run: bool,
    ) -> None:
        marked = Stream.objects.filter(
            realm=realm, can_subscribe_group_id=group.id, deactivated=False
        )
        green_users = list(UserProfile.objects.filter(id__in=green_ids))
        non_green_users = list(UserProfile.objects.filter(id__in=non_green_ids))

        for stream in marked:
            try:
                self.check_and_warn_access_groups(stream, green_ids)
                self.sync_one_channel(stream, green_users, non_green_users, admin_user, dry_run)
            except Exception:
                # PORTAL EDENU: .exception (not %s) — the guards here and in
                # bulk_*_subscriptions are bare asserts, whose str() is "", so a
                # plain %s logs an empty reason. The traceback names the exact line.
                self.logger.exception("✗ Channel '%s' skipped", stream.name)

    def check_and_warn_access_groups(self, stream: Stream, green_ids: set[int]) -> None:
        # can_subscribe_group is the marker (Członkowie) — a Green user in it
        # would read despite the subscription removal, and the group reconcile
        # already strips them; warn anyway for visibility. can_add_subscribers_group
        # is the OTHER content-access bypass; if a Green user sits there the sync
        # cannot fix it from here — flag it loudly.
        if not stream.invite_only:
            self.logger.warning(
                "⚠ '%s' is NOT private — removing a subscription does not block reads. "
                "Make it private (invite-only) for Green protection to work.",
                stream.name,
            )
        bypass_green = set(
            UserGroupMembership.objects.filter(
                user_group_id=stream.can_add_subscribers_group_id,
                user_profile_id__in=green_ids,
            ).values_list("user_profile_id", flat=True)
        )
        for uid in bypass_green:
            user = UserProfile.objects.filter(id=uid).only("delivery_email").first()
            self.logger.warning(
                "⚠ Green user %s can add subscribers to '%s' → bypasses the read block. "
                "Remove them from the channel's 'can_add_subscribers_group'.",
                user.delivery_email if user else f"id={uid}",
                stream.name,
            )

    def sync_one_channel(
        self,
        stream: Stream,
        green_users: list[UserProfile],
        non_green_users: list[UserProfile],
        admin_user: UserProfile,
        dry_run: bool,
    ) -> None:
        # Remove Green subscriptions (the read block on a private channel),
        # add missing non-Green (so the channel lands in their sidebar).
        removed_count = self.remove_subscribers(stream, green_users, admin_user, dry_run)
        added_count = self.add_subscribers(stream, non_green_users, admin_user, dry_run)
        if removed_count or added_count or self.logger.isEnabledFor(logging.DEBUG):
            self.logger.info(
                "  %s: -%d green, +%d non-green", stream.name, removed_count, added_count
            )

    def remove_subscribers(
        self,
        stream: Stream,
        users: list[UserProfile],
        admin_user: UserProfile,
        dry_run: bool,
    ) -> int:
        if not users:
            return 0
        assert stream.recipient_id is not None
        if dry_run:
            existing = Subscription.objects.filter(
                user_profile__in=users, recipient_id=stream.recipient_id, active=True
            ).count()
            return existing
        removed, _not_subscribed = bulk_remove_subscriptions(
            stream.realm,
            users,
            [stream],
            acting_user=admin_user,
            # No "removed from channel" notification to the user — hourly cron,
            # would spam on every run otherwise.
            skip_events_for_removed_user=True,
        )
        return len(removed)

    def add_subscribers(
        self,
        stream: Stream,
        users: list[UserProfile],
        admin_user: UserProfile,
        dry_run: bool,
    ) -> int:
        if not users:
            return 0
        assert stream.recipient_id is not None
        if dry_run:
            subscribed = Subscription.objects.filter(
                user_profile__in=users, recipient_id=stream.recipient_id, active=True
            ).count()
            return len(users) - subscribed
        added, _not_subscribed = bulk_add_subscriptions(
            stream.realm, [stream], users, acting_user=admin_user
        )
        return len(added)

    # -- helpers ------------------------------------------------------------

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
        self.logger = _log
        self.logger.setLevel(logging.DEBUG if verbose else logging.INFO)
        # Keep per-user skips out of Django's root logger (which has mail_admins
        # attached) — mirrors sync_reputation_to_zulip / sync_avatars_from_authentik.
        self.logger.propagate = False
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
            self.logger.addHandler(handler)
