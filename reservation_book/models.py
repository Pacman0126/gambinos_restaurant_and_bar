import datetime
from datetime import date

from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db import models

from django.db.models import JSONField
from django.contrib.auth.models import User

from .constants import SLOT_LABELS
# from django.contrib.postgres.fields import JSONField


class Customer(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(unique=True, null=True, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    mobile = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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


DURATION_CHOICES = [
    (1, '1 hour'),
    (2, '2 hours'),
    (3, '3 hours'),
    (4, '4 hours'),  # ← Now supports 4 hours
]


class TableReservation(models.Model):
    id = models.BigAutoField(primary_key=True)

    # Link to the customer record
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='reservations'
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

    # Slot label, e.g. "17_18"
    time_slot = models.CharField(
        max_length=20,
        help_text="Which time slot was reserved (e.g. '17_18').",
    )

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_reservations',
        help_text="Staff member who created this reservation (for phone bookings)",
    )

    duration_hours = models.PositiveSmallIntegerField(
        choices=DURATION_CHOICES,
        default=1,
        help_text="How many consecutive hours this booking requires (max 4)"
    )

    # How many tables this patron is using in that slot
    number_of_tables_required_by_patron = models.PositiveIntegerField()

    # True = active, False = cancelled
    reservation_status = models.BooleanField(default=True)

    # Distinguish online vs phone-in reservations
    is_phone_reservation = models.BooleanField(
        default=False,
        help_text="True if this reservation was taken over the phone by staff.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def get_time_slot_display(self):
        """Return pretty time slot label (e.g., 18:00–19:00)"""
        from reservation_book.views import SLOT_LABELS
        return SLOT_LABELS.get(self.time_slot, self.time_slot or "Unknown")

    def __str__(self):
        """Human-readable representation for admin and delete confirmation"""
        # If no customer is linked
        if not self.customer:
            return f"Reservation #{self.id} - {self.reservation_date} {self.get_time_slot_display()}"

        # Safe access to customer fields
        name_parts = []
        if self.customer.first_name:
            name_parts.append(self.customer.first_name.strip())
        if self.customer.last_name:
            name_parts.append(self.customer.last_name.strip())

        if name_parts:
            name = " ".join(name_parts)
            return f"{name} - {self.reservation_date} {self.get_time_slot_display()}"

        # Fallback to email
        if self.customer.email:
            return f"{self.customer.email} - {self.reservation_date} {self.get_time_slot_display()}"

        # Ultimate fallback
        return f"Reservation #{self.id} - {self.reservation_date} {self.get_time_slot_display()}"


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
    # Keep this as primary key — correct for your system
    calendar_date = models.DateField(unique=True)

    # --- D E M A N D  (always >= 0, never null) ---
    total_cust_demand_for_tables_17_18 = models.PositiveIntegerField(default=0)
    total_cust_demand_for_tables_18_19 = models.PositiveIntegerField(default=0)
    total_cust_demand_for_tables_19_20 = models.PositiveIntegerField(default=0)
    total_cust_demand_for_tables_20_21 = models.PositiveIntegerField(default=0)
    total_cust_demand_for_tables_21_22 = models.PositiveIntegerField(default=0)

    # --- A V A I L A B I L I T Y  (never null, use default=0 so views can replace with config fallback) ---
    number_of_tables_available_17_18 = models.PositiveIntegerField(default=0)
    number_of_tables_available_18_19 = models.PositiveIntegerField(default=0)
    number_of_tables_available_19_20 = models.PositiveIntegerField(default=0)
    number_of_tables_available_20_21 = models.PositiveIntegerField(default=0)
    number_of_tables_available_21_22 = models.PositiveIntegerField(default=0)

    def _get_default_capacity(self):
        """Pull default from RestaurantConfig, fallback to 10."""
        config = RestaurantConfig.objects.first()
        return config.default_tables_per_slot if config else 10

    def available_for(self, slot: str) -> int:
        """
        Availability rules:
        - If field is 0 (meaning not configured), fallback to restaurant default capacity.
        - Never return None.
        """
        val = getattr(self, f"number_of_tables_available_{slot}", 0)
        return val if val > 0 else self._get_default_capacity()

    def demand_for(self, slot: str) -> int:
        return getattr(self, f"total_cust_demand_for_tables_{slot}", 0)

    def left_for(self, slot: str) -> int:
        return self.available_for(slot) - self.demand_for(slot)

    def save(self, *args, **kwargs):
        """
        On NEW record:
            Any availability fields left as 0 will be populated
            with the default capacity.
        Existing records are left untouched (your demand math remains stable).
        """
        if not self.pk:
            default_tables = self._get_default_capacity()
            for slot in ["17_18", "18_19", "19_20", "20_21", "21_22"]:
                field_name = f"number_of_tables_available_{slot}"
                if getattr(self, field_name) == 0:
                    setattr(self, field_name, default_tables)

        super().save(*args, **kwargs)
