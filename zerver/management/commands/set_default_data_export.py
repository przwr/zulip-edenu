# PORTAL EDENU: Set allow_private_data_export to True in RealmUserDefault
from typing import Any

from django.core.management.base import BaseCommand
from typing_extensions import override

from zerver.models import RealmUserDefault


class Command(BaseCommand):
    help = "PORTAL EDENU: enable allow_private_data_export on every RealmUserDefault"

    @override
    def handle(self, *args: Any, **options: Any) -> None:
        # Idempotent: only rows still False flip; the count reports what changed.
        updated = RealmUserDefault.objects.filter(allow_private_data_export=False).update(
            allow_private_data_export=True
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"PORTAL EDENU: enabled allow_private_data_export on {updated} RealmUserDefault(s)"
            )
        )
