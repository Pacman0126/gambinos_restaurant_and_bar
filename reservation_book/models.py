from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db import models
from django.db.models import JSONField
from django.contrib.auth.models import User
# from django.contrib.postgres.fields import JSONField
import datetime
from datetime import date

# from . import RestaurantConfig
# Create your models here.


# class ContactMobile(models.Model):
#    mobile_phone = PhoneNumberField()


# class ContactPhone(models.Model):
#    phone = PhoneNumberField()


# models.py

# class ReservationBook(models.Model):
#     id = models.AutoField(primary_key=True)
#     reservation_id = models.ForeignKey(
#         "TableReservation",
#         on_delete=models.CASCADE,
#         related_name="reservation_book"
#     )
#     reservation_date = models.DateField()

#     first_name = models.CharField(max_length=100)
#     last_name = models.CharField(max_length=100)

#     phone = PhoneNumberField(blank=True, null=True, region="GB")
#     mobile = PhoneNumberField(blank=True, null=True, region="GB")
#     email = models.EmailField(blank=True, null=True)

#     # Link directly to the Django user who created this reservation
#     created_by = models.ForeignKey(
#         User,
#         on_delete=models.CASCADE,
#         related_name="reservations"
#     )

#     def __str__(self):
#         return f"{self.first_name} {self.last_name} on {self.reservation_date}"


# class TableReservation(models.Model):
#     reservation_id = models.AutoField(primary_key=True)  # auto-increment ID

#     # --- Customer details ---
#     first_name = models.CharField(max_length=100, null=True)
#     last_name = models.CharField(max_length=100, null=True)
#     email = models.EmailField(blank=True, null=True)  # required
#     phone = models.CharField(max_length=20, blank=True, null=True)   # optional
#     mobile = models.CharField(max_length=20, blank=True, null=True)  # optional

#     # --- Reservation details ---
#     time_slot = models.CharField(max_length=15, default='time_slot')
#     number_of_tables_required_by_patron = models.IntegerField(default=0)
#     reservation_status = models.BooleanField(default=True)
#     booked_on_date = models.DateTimeField(auto_now=True)

#     # Link to availability per date
#     timeslot_availability = models.ForeignKey(
#         "TimeSlotAvailability",
#         on_delete=models.CASCADE,
#         to_field="calendar_date",
#         db_column="reservation_date",
#         related_name="reservations",
#     )

#     @property
#     def reservation_date(self):
#         return self.timeslot_availability.calendar_date

#     def __str__(self):
#         return f"Reservation {self.reservation_id} for {self.first_name} {self.last_name} on {self.reservation_date}"
from datetime import date  # <-- make sure this is at the top of models.py


class TableReservation(models.Model):
    id = models.BigAutoField(primary_key=True)

    # Link to the Django user (optional for phone-in customers without an account)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="reservations",
        null=True,
        blank=True,
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

    # How many tables this patron is using in that slot
    number_of_tables_required_by_patron = models.PositiveIntegerField()

    # True = active, False = cancelled
    reservation_status = models.BooleanField(default=True)

    # Distinguish online vs phone-in reservations
    is_phone_reservation = models.BooleanField(
        default=False,
        help_text="True if this reservation was taken over the phone by staff.",
    )

    # --- Patron details ---
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    mobile = models.CharField(max_length=20, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def status_display(self) -> str:
        """
        Derived, human-friendly status for UI:
        - 'Cancelled'  if reservation_status is False
        - 'Completed'  if reservation_status is True but date is in the past
        - 'Active'     otherwise (today or future, or missing date)
        """
        # Cancelled always wins
        if not self.reservation_status:
            return "Cancelled"

        # If we know the date and it's in the past
        if self.reservation_date and self.reservation_date < date.today():
            return "Completed"

        # Otherwise it's an active upcoming/today reservation
        return "Active"

    def __str__(self):
        name = f"{self.first_name} {self.last_name}".strip() or "Guest"
        return f"{name} - {self.time_slot}"


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
    calendar_date = models.DateField(primary_key=True)

    total_cust_demand_for_tables_17_18 = models.IntegerField(default=0)
    number_of_tables_available_17_18 = models.IntegerField(
        null=True, blank=True)
    total_cust_demand_for_tables_18_19 = models.IntegerField(default=0)
    number_of_tables_available_18_19 = models.IntegerField(
        null=True, blank=True)
    total_cust_demand_for_tables_19_20 = models.IntegerField(default=0)
    number_of_tables_available_19_20 = models.IntegerField(
        null=True, blank=True)
    total_cust_demand_for_tables_20_21 = models.IntegerField(default=0)
    number_of_tables_available_20_21 = models.IntegerField(
        null=True, blank=True)
    total_cust_demand_for_tables_21_22 = models.IntegerField(default=0)
    number_of_tables_available_21_22 = models.IntegerField(
        null=True, blank=True)

    def _get_default_capacity(self):
        """Pulls default from RestaurantConfig (fallback 10 if none)."""
        # from . import RestaurantConfig
        config = RestaurantConfig.objects.first()
        return config.default_tables_per_slot if config else 10

    def available_for(self, slot: str) -> int:
        """Return configured availability or fallback default."""
        val = getattr(self, f"number_of_tables_available_{slot}")
        return val if val not in (None, 0) else self._get_default_capacity()

    def demand_for(self, slot: str) -> int:
        return getattr(self, f"total_cust_demand_for_tables_{slot}")

    def left_for(self, slot: str) -> int:
        return self.available_for(slot) - self.demand_for(slot)

    def save(self, *args, **kwargs):
        # Only set defaults if new record AND field not provided
        if not self.pk:
            default_tables = self._get_default_capacity()
            for slot in ["17_18", "18_19", "19_20", "20_21", "21_22"]:
                field_name = f"number_of_tables_available_{slot}"
                if getattr(self, field_name) in (None, 0):
                    setattr(self, field_name, default_tables)
        super().save(*args, **kwargs)
