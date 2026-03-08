from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from reservation_book.models import TableReservation, TimeSlotAvailability
from reservation_book.views import SLOT_LABELS, _affected_slots


class Command(BaseCommand):
    help = (
        "Reset TimeSlotAvailability demand counters for one or more dates, "
        "with optional rebuild from active future reservations."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "dates",
            nargs="*",
            help="Dates to reset in YYYY-MM-DD format, e.g. 2026-03-09 2026-03-10",
        )
        parser.add_argument(
            "--from-date",
            dest="from_date",
            help="Start date in YYYY-MM-DD format",
        )
        parser.add_argument(
            "--to-date",
            dest="to_date",
            help="End date in YYYY-MM-DD format",
        )
        parser.add_argument(
            "--all-next-30",
            action="store_true",
            help="Reset today through today+29",
        )
        parser.add_argument(
            "--rebuild-active-future",
            action="store_true",
            help=(
                "After reset, rebuild demand from ACTIVE reservations on the same dates. "
                "Past, cancelled, completed, and no-show reservations are ignored."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be changed without saving",
        )

    def _parse_date(self, value):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise CommandError(
                f"Invalid date '{value}'. Use YYYY-MM-DD.") from exc

    def _collect_dates(self, options):
        dates = set()

        if options["dates"]:
            for raw in options["dates"]:
                dates.add(self._parse_date(raw))

        from_date = options.get("from_date")
        to_date = options.get("to_date")

        if from_date and not to_date:
            raise CommandError("--from-date requires --to-date")
        if to_date and not from_date:
            raise CommandError("--to-date requires --from-date")

        if from_date and to_date:
            start = self._parse_date(from_date)
            end = self._parse_date(to_date)
            if end < start:
                raise CommandError("--to-date must be on or after --from-date")

            cur = start
            while cur <= end:
                dates.add(cur)
                cur += timedelta(days=1)

        if options["all_next_30"]:
            from django.utils import timezone
            start = timezone.localdate()
            for i in range(30):
                dates.add(start + timedelta(days=i))

        if not dates:
            raise CommandError(
                "Provide at least one date, a --from-date/--to-date range, or --all-next-30."
            )

        return sorted(dates)

    def _demand_fields(self):
        return [f"total_cust_demand_for_tables_{slot}" for slot in SLOT_LABELS.keys()]

    def _reset_ts_row(self, ts, dry_run=False):
        demand_fields = self._demand_fields()
        before = {field: getattr(ts, field, 0) for field in demand_fields}

        if not dry_run:
            for field in demand_fields:
                setattr(ts, field, 0)
            ts.save(update_fields=demand_fields)

        return before

    def _rebuild_for_date(self, target_date, dry_run=False):
        """
        Rebuild demand from ACTIVE reservations on target_date only.
        """
        reservations = (
            TableReservation.objects
            .filter(
                reservation_date=target_date,
                status=getattr(TableReservation, "STATUS_ACTIVE", "active"),
            )
            .select_related("timeslot_availability")
            .order_by("id")
        )

        rebuilt_count = 0

        for reservation in reservations:
            tables_needed = int(
                reservation.number_of_tables_required_by_patron or 0)
            duration = int(reservation.duration_hours or 1)
            slots = _affected_slots(
                reservation.time_slot,
                duration,
                until_close=False,
            )

            ts = reservation.timeslot_availability
            if ts is None or ts.calendar_date != target_date:
                ts, _ = TimeSlotAvailability.objects.get_or_create(
                    calendar_date=target_date
                )

            update_fields = []

            for slot in slots:
                field = f"total_cust_demand_for_tables_{slot}"
                current = int(getattr(ts, field, 0) or 0)
                new_value = current + tables_needed
                if not dry_run:
                    setattr(ts, field, new_value)
                update_fields.append(field)

            if not dry_run and update_fields:
                ts.save(update_fields=update_fields)

            rebuilt_count += 1

        return rebuilt_count

    @transaction.atomic
    def handle(self, *args, **options):
        dates = self._collect_dates(options)
        dry_run = options["dry_run"]
        rebuild = options["rebuild_active_future"]

        self.stdout.write(self.style.WARNING(
            f"Target dates: {', '.join(str(d) for d in dates)}"))
        if dry_run:
            self.stdout.write(self.style.WARNING(
                "DRY RUN ONLY — no changes will be saved."))

        for target_date in dates:
            ts, created = TimeSlotAvailability.objects.get_or_create(
                calendar_date=target_date)

            before = self._reset_ts_row(ts, dry_run=dry_run)

            self.stdout.write("")
            self.stdout.write(self.style.NOTICE(f"{target_date}"))
            self.stdout.write(
                f"  Row existed: {'no, created now' if created else 'yes'}")

            non_zero_before = {k: v for k,
                               v in before.items() if int(v or 0) != 0}
            if non_zero_before:
                self.stdout.write("  Demand before reset:")
                for field, value in non_zero_before.items():
                    self.stdout.write(f"    {field} = {value}")
            else:
                self.stdout.write("  Demand before reset: already all zero")

            self.stdout.write(
                "  Demand after reset: all slot-demand fields set to 0")

            if rebuild:
                rebuilt_count = self._rebuild_for_date(
                    target_date, dry_run=dry_run)
                self.stdout.write(
                    f"  Rebuild from ACTIVE reservations on {target_date}: {rebuilt_count} reservation(s) reapplied"
                )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("reset_demand completed."))
