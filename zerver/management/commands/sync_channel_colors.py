# PORTAL EDENU: one-shot command to unify every user's channel (stream)
# subscription color with the stream's folder accent color, so the sidebar
# swatch matches the folder card color for all users (not just the owner).
#
# New subscriptions already inherit the folder color via the pick_colors hook
# in zerver/actions/streams.py; this command back-fills existing subscriptions.
#
# Streams without a folder are left untouched.

from django.core.management.base import BaseCommand
from django.db import transaction
from typing_extensions import override

from zerver.lib.portal_colors import folder_color as portal_folder_color
from zerver.models import Subscription
from zerver.models.channel_folders import ChannelFolder
from zerver.models.streams import Stream


class Command(BaseCommand):
    help = "Set every user's channel subscription color to the stream's folder accent color"

    @override
    def handle(self, *args: str, **options: str) -> None:
        # folder_id -> accent color (one lookup per folder, ~12 total).
        folder_color_by_id: dict[int, str] = {
            folder.id: portal_folder_color(folder.name, folder.id)
            for folder in ChannelFolder.objects.all()
        }

        # recipient_id -> folder color, only for streams that have a folder.
        # values_list yields plain ints (django-stubs types stream.recipient_id
        # as int | None, which won't narrow; values_list sidesteps that).
        stream_colors: dict[int, str] = {}
        for folder_id, color in folder_color_by_id.items():
            for recipient_id in Stream.objects.filter(folder_id=folder_id).values_list(
                "recipient_id", flat=True
            ):
                stream_colors[recipient_id] = color

        if not stream_colors:
            self.stdout.write("No streams are assigned to a folder; nothing to do.")
            return

        updated = 0
        with transaction.atomic(durable=True):
            for recipient_id, color in stream_colors.items():
                # Bulk update all subscriptions to this stream at once.
                updated += Subscription.objects.filter(recipient_id=recipient_id).update(
                    color=color
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Updated {updated} subscription(s) across {len(stream_colors)} foldered stream(s)."
            )
        )
