from django.core.management.base import BaseCommand
from django.utils import timezone

from reservation_book.services.sweeps import run_no_show_sweep


class Command(BaseCommand):
    help = "Mark past active reservations as NO_SHOW and update customer counters/ban flag."

    def add_arguments(self, parser):
        parser.add_argument("--date", type=str, default=None,
                            help="Override today's date (YYYY-MM-DD).")
        parser.add_argument("--threshold", type=int, default=3,
                            help="No-show ban threshold (default 3).")

    def handle(self, *args, **options):
        date_str = options["date"]
        threshold = options["threshold"]

        today = timezone.localdate()
        if date_str:
            today = timezone.datetime.fromisoformat(date_str).date()

        result = run_no_show_sweep(today=today, ban_threshold=threshold)

        self.stdout.write(self.style.SUCCESS(
            f"Sweep complete for {today}: scanned={result.scanned}, "
            f"marked_no_show={result.marked_no_show}, barred_customers={result.barred_customers}"
        ))
