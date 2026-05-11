# PORTAL EDENU: Set allow_private_data_export to True in RealmUserDefault
from typing import Any

from django.core.management.base import BaseCommand

from zerver.models import RealmUserDefault


class Command(BaseCommand):
    help = "PORTAL EDENU: Set allow_private_data_export to True for RealmUserDefault"

    def handle(self, *args: Any, **options: Any) -> None:
        self.stdout.write("=" * 70)
        self.stdout.write("PORTAL EDENU: Updating RealmUserDefault allow_private_data_export")
        self.stdout.write("=" * 70)

        ruds = RealmUserDefault.objects.select_related("realm").all()

        for rud in ruds:
            self.stdout.write(f"\nRealm: {rud.realm.string_id} (ID: {rud.realm.id})")
            self.stdout.write(f"  Current allow_private_data_export: {rud.allow_private_data_export}")

            if not rud.allow_private_data_export:
                rud.allow_private_data_export = True
                rud.save(update_fields=["allow_private_data_export"])
                self.stdout.write(self.style.SUCCESS(f"  Updated to: True"))
            else:
                self.stdout.write(self.style.WARNING("  Already set to True (no change needed)"))

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write("Update complete!")
        self.stdout.write("=" * 70)
