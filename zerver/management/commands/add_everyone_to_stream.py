# PORTAL EDENU: one-shot command to subscribe every user in a realm to a
# channel, including deactivated (inactive) users. The stock
# add_users_to_streams --all-users command skips them (get_users filters
# is_active=True unless include_deactivated=True, and it never passes that).
#
# Usage (defaults to the root realm '' — pass --realm=internal for the internal one):
#   ./manage.py add_everyone_to_stream --stream="Wspólne podróże 🎒"

from typing import Any

from django.core.management.base import CommandParser
from typing_extensions import override

from zerver.actions.streams import bulk_add_subscriptions
from zerver.lib.management import ZulipBaseCommand
from zerver.lib.streams import ensure_stream
from zerver.lib.utils import assert_is_not_none
from zerver.models import Realm, Subscription, UserProfile


class Command(ZulipBaseCommand):
    help = "Add every user in a realm to a stream, including deactivated users (bots too)."

    @override
    def add_arguments(self, parser: CommandParser) -> None:
        self.add_realm_args(parser)  # optional; defaults to the root realm
        parser.add_argument("-s", "--stream", required=True, help="Name of the stream")

    @override
    def handle(self, *args: Any, **options: Any) -> None:
        realm = self.get_realm(options)
        if realm is None:
            # No --realm given: this server has realms '' (root, default) and
            # 'internal'; default to the root one.
            realm = Realm.objects.get(string_id="")

        stream = ensure_stream(realm, options["stream"], acting_user=None)
        # No is_active filter is the whole point of this command.
        users = list(UserProfile.objects.filter(realm=realm))

        # Respect explicit unsubscribes: bulk_add_subscriptions would
        # reactivate their inactive subscription rows, so skip them.
        unsubscribed_ids = set(
            Subscription.objects.filter(
                recipient_id=assert_is_not_none(stream.recipient_id), active=False
            ).values_list("user_profile_id", flat=True)
        )
        users = [u for u in users if u.id not in unsubscribed_ids]

        subscribed, already_subscribed = bulk_add_subscriptions(
            realm, [stream], users, acting_user=None
        )
        print(
            f"[{realm.string_id}] '{stream.name}': {len(subscribed)} newly subscribed, "
            f"{len(already_subscribed)} already subscribed, {len(unsubscribed_ids)} skipped (unsubscribed)"
        )
