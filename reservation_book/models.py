from __future__ import annotations

from django.conf import settings
import datetime
from datetime import date
from django.utils import timezone

from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db import models

from django.db.models import JSONField
from django.contrib.auth.models import User

from .constants import SLOT_LABELS
# from django.contrib.postgres.fields import JSONField

DURATION_CHOICES = [
    (1, '1 hour'),
    (2, '2 hours'),
    (3, '3 hours'),
    (4, '4 hours'),  # ← Now supports 4 hours
]

# Canonical ordering for walking duration_hours forward
SLOT_KEYS = list(SLOT_LABELS.keys())


def _slot_order() -> list[str]:
    """
    Canonical slot ordering for duration calculations.
    Keeps it in models.py so properties don't import views.py.
    """
    return list(SLOT_KEYS) if SLOT_KEYS else list(SLOT_LABELS.keys())


class Customer(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(unique=True, null=True, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    mobile = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # number_of_tables_required_by_patron = models.PositiveIntegerField(
    #     default=1)

    # New: Flag for barred/banned customers
    barred = models.BooleanField(
        default=False,
        help_text="Check if this customer is not welcome (no new bookings allowed)"
    )

    # Enhanced notes field with examples
    notes = models.TextField(
        blank=True,
        help_text="Staff notes: e.g., VIP, regular, allergy, restaurant critic, supplier, chef, etc."
    )

    def __str__(self):
        base = f"{self.first_name} {self.last_name}".strip()
        if not base:
            base = self.email or self.phone or self.mobile or "Unknown Customer"
        if self.barred:
            base += " [BARRED]"
        return base

    class Meta:
        ordering = ['last_name', 'first_name']
        verbose_name = "Customer"
        verbose_name_plural = "Customers"


# -------------------------------------------------------------------
# QuerySet + Manager (THIS is what makes `.active()` exist)
# -------------------------------------------------------------------

class TableReservationQuerySet(models.QuerySet):
    """
    Reservation QuerySet helpers.

    IMPORTANT:
    - Supports your new lifecycle `status` field.
    - Also supports legacy `reservation_status` boolean (backward compatibility).
    """

    # def active(self):
    #     """
    #     Active reservations only.

    #     Logic:
    #     - If `status` exists, filter status=active
    #     - If legacy `reservation_status` exists, also require reservation_status=True
    #     """
    #     qs = self
    #     model = self.model

    #     if hasattr(model, "status") and hasattr(model, "STATUS_ACTIVE"):
    #         qs = qs.filter(status=model.STATUS_ACTIVE)

    #     if hasattr(model, "reservation_status"):
    #         qs = qs.filter(reservation_status=True)

    #     return qs
    def active(self):
        """
        Allows chaining: TableReservation.objects.filter(...).active()
        Works for both new status field or legacy boolean.
        """
        model = self.model

        if hasattr(model, "STATUS_ACTIVE") and hasattr(model, "status"):
            return self.filter(status=model.STATUS_ACTIVE)

        if hasattr(model, "reservation_status"):
            return self.filter(reservation_status=True)

        return self

    def cancelled(self):
        model = self.model

        if hasattr(model, "STATUS_CANCELLED") and hasattr(model, "status"):
            return self.filter(status=model.STATUS_CANCELLED)

        if hasattr(model, "reservation_status"):
            return self.filter(reservation_status=False)

        return self

    # def cancelled(self):
    #     qs = self
    #     model = self.model

    #     if hasattr(model, "status") and hasattr(model, "STATUS_CANCELLED"):
    #         qs = qs.filter(status=model.STATUS_CANCELLED)
    #     elif hasattr(model, "reservation_status"):
    #         qs = qs.filter(reservation_status=False)

    #     return qs

    def historical(self):
        """
        Anything not-active (cancelled/completed/no_show) if you use status.
        If only legacy boolean exists, historical==reservation_status=False.
        """
        model = self.model

        if hasattr(model, "status") and hasattr(model, "STATUS_ACTIVE"):
            return self.exclude(status=model.STATUS_ACTIVE)

        if hasattr(model, "reservation_status"):
            return self.filter(reservation_status=False)

        return self.none()


class TableReservationManager(models.Manager.from_queryset(TableReservationQuerySet)):
    """
    Manager is now *backed by* TableReservationQuerySet.
    That means any queryset created from objects will have .active().
    """
    pass


# This single Manager is the only one you should have.
# It automatically returns a TableReservationQuerySet so `.active()` works.
TableReservationManager = models.Manager.from_queryset(
    TableReservationQuerySet)
# -------------------------------------------------------------------
# TableReservation model (your status system + legacy boolean kept)
# -------------------------------------------------------------------


class TableReservation(models.Model):
    # Attach the working manager (THIS enables `.active()` everywhere)
    objects = TableReservationManager()

    id = models.BigAutoField(primary_key=True)

    # ----------------------------
    # Status choices (new system)
    # ----------------------------
    STATUS_ACTIVE = "active"
    STATUS_CANCELLED = "cancelled"
    STATUS_COMPLETED = "completed"
    STATUS_NO_SHOW = "no_show"

    STATUS_CHOICES = (
        (STATUS_ACTIVE, "Active"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_NO_SHOW, "No-show"),
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
        db_index=True,
        help_text="Reservation lifecycle status for operations and reporting.",
    )

    # For reporting/auditing
    cancelled_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Link to the customer record
    customer = models.ForeignKey(
        "Customer",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="reservations",
    )

    # Link to the date + table availability record
    timeslot_availability = models.ForeignKey(
        "TimeSlotAvailability",
        on_delete=models.CASCADE,
        related_name="reservations",
    )

    # Denormalized date for convenience (mirrors timeslot_availability.calendar_date)
    reservation_date = models.DateField(
        null=True,
        blank=True,
        help_text="Denormalized date from the related TimeSlotAvailability.",
    )

    # Slot key, e.g. "17_18"
    time_slot = models.CharField(
        max_length=20,
        help_text="Which time slot was reserved (e.g. '17_18').",
    )

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_reservations",
        help_text="Staff member who created this reservation (for phone bookings).",
    )

    # Your choices constant should already exist in your file; keep yours.
    # If you already have DURATION_CHOICES above, this line will work as-is.
    duration_hours = models.PositiveSmallIntegerField(
        choices=getattr(settings, "DURATION_CHOICES", None) or (
            (1, "1 hour"),
            (2, "2 hours"),
            (3, "3 hours"),
            (4, "4 hours"),
        ),
        default=1,
        help_text="How many consecutive hours this booking requires (max 4).",
    )

    series = models.ForeignKey(
        "ReservationSeries",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reservations",
        help_text="If set, this reservation is part of a multi-day series booking.",
    )

    # How many tables this patron is using in that slot
    number_of_tables_required_by_patron = models.PositiveIntegerField(
        default=1)

    # ----------------------------
    # Legacy boolean (keep for now)
    # ----------------------------
    # True = active, False = cancelled (legacy)
    reservation_status = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Legacy flag kept for backward compatibility; prefer `status`.",
    )

    # Distinguish online vs phone-in reservations
    is_phone_reservation = models.BooleanField(
        default=False,
        help_text="True if this reservation was taken over the phone by staff.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ----------------------------
    # Display helpers
    # ----------------------------

    @property
    def time_range_pretty(self) -> str:
        """
        Returns a human-readable time range like:
        17:00–21:00 for multi-hour bookings

        IMPORTANT:
        - No importing views.py here.
        - Uses SLOT_LABELS + SLOT_KEYS from models.py.
        """
        slots = _slot_order()
        if not self.time_slot:
            return "-"

        if self.time_slot not in slots:
            return SLOT_LABELS.get(self.time_slot, self.time_slot)

        start_index = slots.index(self.time_slot)

        try:
            dur = int(self.duration_hours or 1)
        except Exception:
            dur = 1
        dur = max(dur, 1)

        end_index = min(start_index + dur - 1, len(slots) - 1)

        start_label = SLOT_LABELS.get(slots[start_index], self.time_slot)
        end_label = SLOT_LABELS.get(slots[end_index], slots[end_index])

        try:
            start_time = start_label.split("–")[0].strip()
            end_time = end_label.split("–")[1].strip()
            return f"{start_time}–{end_time}"
        except Exception:
            return start_label

    def get_time_slot_display(self) -> str:
        """Return pretty single-hour label (e.g., 18:00–19:00)."""
        return SLOT_LABELS.get(self.time_slot, self.time_slot or "Unknown")

    @property
    def is_active(self) -> bool:
        """
        True only when the reservation is currently active.
        Used by staff views, customer views, and availability math.
        """
        return self.status == self.STATUS_ACTIVE and self.reservation_status is True

    @property
    def status_display(self) -> str:
        """
        Human-friendly status label for templates.
        Matches what your templates expect: "Active", "Cancelled", "Completed", "No-show".
        """
        lookup = dict(self.STATUS_CHOICES)
        return lookup.get(self.status, self.status or "-")

    # --------------------------------
    # Lifecycle helpers (NOT properties)
    # --------------------------------
    def mark_cancelled(self) -> None:
        """
        Mark reservation as cancelled (does NOT touch availability).
        Availability release is handled elsewhere.
        """
        self.status = self.STATUS_CANCELLED
        self.cancelled_at = timezone.now()

        # Backward compatibility
        self.reservation_status = False

    def mark_completed(self) -> None:
        """Mark reservation as completed (guest arrived and was served)."""
        self.status = self.STATUS_COMPLETED
        self.completed_at = timezone.now()

    def mark_no_show(self) -> None:
        """Mark reservation as no-show (guest never arrived)."""
        self.status = self.STATUS_NO_SHOW

    def __str__(self) -> str:
        """
        Human-readable representation for admin and delete confirmation.
        """
        when = f"{self.reservation_date} {self.get_time_slot_display()}"
        if not self.customer:
            return f"Reservation #{self.id} - {when}"

        first = (getattr(self.customer, "first_name", "") or "").strip()
        last = (getattr(self.customer, "last_name", "") or "").strip()
        name = (first + " " + last).strip()

        if name:
            return f"{name} - {when}"

        email = (getattr(self.customer, "email", "") or "").strip()
        if email:
            return f"{email} - {when}"

        return f"Reservation #{self.id} - {when}"
# class TableReservation(models.Model):
#     id = models.BigAutoField(primary_key=True)
#     objects = TableReservationManager()

#     # ----------------------------
#     # Status choices
#     # ----------------------------
#     STATUS_ACTIVE = "active"
#     STATUS_CANCELLED = "cancelled"
#     STATUS_COMPLETED = "completed"
#     STATUS_NO_SHOW = "no_show"

#     STATUS_CHOICES = (
#         (STATUS_ACTIVE, "Active"),
#         (STATUS_CANCELLED, "Cancelled"),
#         (STATUS_COMPLETED, "Completed"),
#         (STATUS_NO_SHOW, "No-show"),
#     )

#     status = models.CharField(
#         max_length=20,
#         choices=STATUS_CHOICES,
#         default=STATUS_ACTIVE,
#         db_index=True,
#         help_text="Reservation lifecycle status for operations and reporting.",
#     )

#     cancelled_at = models.DateTimeField(null=True, blank=True)
#     completed_at = models.DateTimeField(null=True, blank=True)

#     customer = models.ForeignKey(
#         "Customer",
#         on_delete=models.CASCADE,
#         null=True,
#         blank=True,
#         related_name="reservations",
#     )

#     timeslot_availability = models.ForeignKey(
#         "TimeSlotAvailability",
#         on_delete=models.CASCADE,
#         related_name="reservations",
#     )

#     reservation_date = models.DateField(
#         null=True,
#         blank=True,
#         help_text="Denormalized date from the related TimeSlotAvailability.",
#     )

#     time_slot = models.CharField(
#         max_length=20,
#         help_text="Which time slot was reserved (e.g. '17_18').",
#     )

#     created_by = models.ForeignKey(
#         "auth.User",
#         on_delete=models.SET_NULL,
#         null=True,
#         blank=True,
#         related_name="created_reservations",
#         help_text="Staff member who created this reservation (for phone bookings)",
#     )

#     duration_hours = models.PositiveSmallIntegerField(
#         choices=DURATION_CHOICES,
#         default=1,
#         help_text="How many consecutive hours this booking requires (max 4)",
#     )

#     series = models.ForeignKey(
#         "ReservationSeries",
#         on_delete=models.SET_NULL,
#         null=True,
#         blank=True,
#         related_name="reservations",
#         help_text="If set, this reservation is part of a multi-day series booking.",
#     )

#     number_of_tables_required_by_patron = models.PositiveIntegerField(
#         default=1)

#     # Legacy boolean (keep for now if you’re still using it in views/templates/admin)
#     reservation_status = models.BooleanField(default=True)

#     is_phone_reservation = models.BooleanField(
#         default=False,
#         help_text="True if this reservation was taken over the phone by staff.",
#     )

#     created_at = models.DateTimeField(auto_now_add=True)
#     updated_at = models.DateTimeField(auto_now=True)

#     @property
#     def time_range_pretty(self):
#         """
#         Returns a human-readable time range like:
#             17:00–21:00 for multi-hour bookings.

#         IMPORTANT:
#         - This is a read-only @property; DO NOT assign to it in views.
#         - Do NOT import views here (prevents circular imports).
#         """
#         SLOT_LABELS_LOCAL = globals().get("SLOT_LABELS", {})
#         SLOT_KEYS_LOCAL = globals().get("SLOT_KEYS", list(SLOT_LABELS_LOCAL.keys()))

#         start_slot = getattr(self, "time_slot", None)
#         if not start_slot:
#             return "-"

#         start_label = SLOT_LABELS_LOCAL.get(start_slot, start_slot)

#         try:
#             dur = int(getattr(self, "duration_hours", 1) or 1)
#         except Exception:
#             dur = 1

#         if dur <= 1:
#             return start_label

#         if start_slot not in SLOT_KEYS_LOCAL:
#             return start_label

#         start_index = SLOT_KEYS_LOCAL.index(start_slot)
#         end_index = min(start_index + dur - 1, len(SLOT_KEYS_LOCAL) - 1)
#         end_slot = SLOT_KEYS_LOCAL[end_index]
#         end_label = SLOT_LABELS_LOCAL.get(end_slot, end_slot)

#         try:
#             start_time = start_label.split("–")[0].strip()
#             end_time = end_label.split("–")[1].strip()
#             return f"{start_time}–{end_time}"
#         except Exception:
#             return start_label

#     @property
#     def is_active(self) -> bool:
#         """
#         True only when the reservation is currently active.
#         Used by staff views, customer views, and availability math.
#         """
#         return self.status == self.STATUS_ACTIVE

#     # --------------------------------
#     # Lifecycle helpers (NOT properties)
#     # --------------------------------
#     def mark_cancelled(self) -> None:
#         """
#         Mark reservation as cancelled (does NOT touch availability).
#         Availability release is handled elsewhere.
#         """
#         self.status = self.STATUS_CANCELLED
#         self.cancelled_at = timezone.now()

#         # Backward compatibility with legacy boolean
#         if hasattr(self, "reservation_status"):
#             self.reservation_status = False

#     def mark_completed(self) -> None:
#         self.status = self.STATUS_COMPLETED
#         self.completed_at = timezone.now()

#     def mark_no_show(self) -> None:
#         self.status = self.STATUS_NO_SHOW

#     # ✅ THIS is the critical line: it wires QuerySet + Manager together
#     objects = TableReservationManager()


class RestaurantConfig(models.Model):
    default_tables_per_slot = models.PositiveIntegerField(default=10)

    class Meta:
        verbose_name = "Restaurant Configuration"
        verbose_name_plural = "Restaurant Configuration"

    def __str__(self):
        return f"Config (Default Tables: {self.default_tables_per_slot})"

    # remove or ignore total_cust_demand_for_tables_* for pure dynamic

    def demand_for(self, slot):
        return TableReservation.objects.filter(
            timeslot_availability=self,
            time_slot=slot,
            reservation_status=True
        ).aggregate(total=models.Sum("number_of_tables_required_by_patron"))["total"] or 0

    def left_for(self, slot):
        available = getattr(self, f"number_of_tables_available_{slot}")
        return available - self.demand_for(slot)


class TimeSlotAvailability(models.Model):
    # Back to the original schema your DB actually has:
    calendar_date = models.DateField(primary_key=True)

    # --- D E M A N D ---
    total_cust_demand_for_tables_17_18 = models.PositiveIntegerField(default=0)
    total_cust_demand_for_tables_18_19 = models.PositiveIntegerField(default=0)
    total_cust_demand_for_tables_19_20 = models.PositiveIntegerField(default=0)
    total_cust_demand_for_tables_20_21 = models.PositiveIntegerField(default=0)
    total_cust_demand_for_tables_21_22 = models.PositiveIntegerField(default=0)

    # --- A V A I L A B I L I T Y ---
    number_of_tables_available_17_18 = models.PositiveIntegerField(default=0)
    number_of_tables_available_18_19 = models.PositiveIntegerField(default=0)
    number_of_tables_available_19_20 = models.PositiveIntegerField(default=0)
    number_of_tables_available_20_21 = models.PositiveIntegerField(default=0)
    number_of_tables_available_21_22 = models.PositiveIntegerField(default=0)

    def _get_default_capacity(self):
        config = RestaurantConfig.objects.first()
        return config.default_tables_per_slot if config else 10

    def available_for(self, slot: str) -> int:
        val = getattr(self, f"number_of_tables_available_{slot}", 0) or 0
        return val if val > 0 else self._get_default_capacity()

    def demand_for(self, slot: str) -> int:
        return getattr(self, f"total_cust_demand_for_tables_{slot}", 0) or 0

    def left_for(self, slot: str) -> int:
        left = self.available_for(slot) - self.demand_for(slot)
        return left if left > 0 else 0

    def save(self, *args, **kwargs):
        # On NEW record: fill any 0 availability fields with default capacity
        if self._state.adding:
            default_tables = self._get_default_capacity()
            for slot in ["17_18", "18_19", "19_20", "20_21", "21_22"]:
                field_name = f"number_of_tables_available_{slot}"
                if (getattr(self, field_name, 0) or 0) == 0:
                    setattr(self, field_name, default_tables)
        super().save(*args, **kwargs)


class ReservationSeries(models.Model):
    """
    Groups multiple TableReservation rows into one 'series' booking
    (e.g. conference: 18:00–20:00 for 4 consecutive days).
    """
    customer = models.ForeignKey(
        "Customer",
        on_delete=models.CASCADE,
        related_name="reservation_series",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_series",
    )
    title = models.CharField(max_length=200, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        label = self.title.strip() or "Series"
        return f"{label} (#{self.pk})"
