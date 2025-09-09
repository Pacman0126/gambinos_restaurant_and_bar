from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db import models
from django.db.models import JSONField
from django.contrib.auth.models import User
# from django.contrib.postgres.fields import JSONField
import datetime
from datetime import date


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


class TableReservation(models.Model):
    reservation_id = models.AutoField(primary_key=True)  # auto-increment ID

    # --- Customer details ---
    first_name = models.CharField(max_length=100, null=True)
    last_name = models.CharField(max_length=100, null=True)
    email = models.EmailField(blank=True, null=True)  # required
    phone = models.CharField(max_length=20, blank=True, null=True)   # optional
    mobile = models.CharField(max_length=20, blank=True, null=True)  # optional

    # --- Reservation details ---
    time_slot = models.CharField(max_length=15, default='time_slot')
    number_of_tables_required_by_patron = models.IntegerField(default=0)
    reservation_status = models.BooleanField(default=True)
    booked_on_date = models.DateTimeField(auto_now=True)

    # Link to availability per date
    timeslot_availability = models.ForeignKey(
        "TimeSlotAvailability",
        on_delete=models.CASCADE,
        to_field="calendar_date",
        db_column="reservation_date",
        related_name="reservations",
    )

    @property
    def reservation_date(self):
        return self.timeslot_availability.calendar_date

    def __str__(self):
        return f"Reservation {self.reservation_id} for {self.first_name} {self.last_name} on {self.reservation_date}"


@require_POST
def cancel_reservation(request, reservation_id):
    """Cancel a reservation and free up tables."""
    try:
        reservation = TableReservation.objects.get(pk=reservation_id)

        # Update demand count (free up tables)
        ts = reservation.timeslot_availability
        demand_field = f"total_cust_demand_for_tables_{reservation.time_slot}"
        setattr(ts, demand_field, getattr(ts, demand_field) -
                reservation.number_of_tables_required_by_patron)
        ts.save()

        reservation.delete()
        return JsonResponse({"success": True})

    except TableReservation.DoesNotExist:
        return JsonResponse({"success": False, "error": "Reservation not found."}, status=404)


class TimeSlotAvailability(models.Model):
    calendar_date = models.DateField(primary_key=True)

    total_cust_demand_for_tables_17_18 = models.IntegerField(default=0)
    number_of_tables_available_17_18 = models.IntegerField(default=10)
    total_cust_demand_for_tables_18_19 = models.IntegerField(default=0)
    number_of_tables_available_18_19 = models.IntegerField(default=10)
    total_cust_demand_for_tables_19_20 = models.IntegerField(default=0)
    number_of_tables_available_19_20 = models.IntegerField(default=10)
    total_cust_demand_for_tables_20_21 = models.IntegerField(default=0)
    number_of_tables_available_20_21 = models.IntegerField(default=10)
    total_cust_demand_for_tables_21_22 = models.IntegerField(default=0)
    number_of_tables_available_21_22 = models.IntegerField(default=10)

    def __str__(self):
        return f"Availability for {self.calendar_date}"
