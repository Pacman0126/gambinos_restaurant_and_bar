from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from reservation_book.models import Customer, NoShowEvent, TableReservation


@dataclass
class NoShowSweepResult:
    scanned: int
    marked_no_show: int
    barred_customers: int


DEFAULT_NO_SHOW_BAN_THRESHOLD = 3


def run_no_show_sweep(
    *,
    today=None,
    ban_threshold: int = DEFAULT_NO_SHOW_BAN_THRESHOLD,
) -> NoShowSweepResult:
    """
    Marks past ACTIVE reservations as NO_SHOW.
    Creates NoShowEvent and updates Customer counters/barred flag.

    Safe defaults:
    - Uses `reservation_date` (denormalized) for sweep logic.
    - Uses status as the source of truth.
    - Does not assume optional NoShowEvent fields exist.
    """
    if today is None:
        today = timezone.localdate()

    barred_count = 0
    marked_count = 0

    with transaction.atomic():
        qs = (
            TableReservation.objects
            .select_for_update(of=("self",))
            .filter(
                status=TableReservation.STATUS_ACTIVE,
                reservation_date__lt=today,
            )
            .order_by("reservation_date", "time_slot", "id")
        )

        reservations = list(qs)
        scanned = len(reservations)

        # Detect optional field on NoShowEvent (you previously had marked_by_staff confusion)
        has_marked_by_staff = any(
            f.name == "marked_by_staff"
            for f in NoShowEvent._meta.get_fields()
            if hasattr(f, "name")
        )

        for r in reservations:

            cust_email = ""
            if r.customer_id:
                cust_email = (Customer.objects.filter(pk=r.customer_id)
                              .values_list("email", flat=True).first() or "").strip()

            event_kwargs = dict(
                reservation_id=r.id,
                reservation_date=r.reservation_date,
                time_slot=r.time_slot or "",
                tables=int(
                    getattr(r, "number_of_tables_required_by_patron", 0) or 0),
                duration_slots=int(getattr(r, "duration_hours", 1) or 1),
                customer_email=cust_email,
            )
            if has_marked_by_staff:
                event_kwargs["marked_by_staff"] = False  # sweep-generated

            NoShowEvent.objects.create(**event_kwargs)

            # Mark reservation as no-show
            r.status = TableReservation.STATUS_NO_SHOW
            r.save(update_fields=["status"])
            marked_count += 1

            # Update customer counters + barred flag
            if r.customer_id:
                Customer.objects.filter(pk=r.customer_id).update(
                    no_show_count=F("no_show_count") + 1
                )
                c = Customer.objects.select_for_update().get(pk=r.customer_id)
                if (not c.barred) and c.no_show_count >= ban_threshold:
                    c.barred = True
                    c.save(update_fields=["barred"])
                    barred_count += 1

    return NoShowSweepResult(scanned=scanned, marked_no_show=marked_count, barred_customers=barred_count)
