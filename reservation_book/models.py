from django.db import models
from django.db.models import JSONField
from django.contrib.auth.models import User
# from django.contrib.postgres.fields import JSONField
from phonenumber_field.modelfields import PhoneNumberField
from phonenumber_field.phonenumber import PhoneNumber
import datetime
from datetime import date


# Create your models here.


class ContactMobile(models.Model):
    mobile_phone = PhoneNumberField()


class ContactPhone(models.Model):
    phone = PhoneNumberField()


class ReservationBook(models.Model):

    reservation_id = models.AutoField(primary_key=True)
    reservation_id = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        primary_key=True,
        unique=True
    )
    reservation_date = models.DateField(default=date.today)
    first_name = models.CharField(max_length=15, default='first name')
    last_name = models.CharField(max_length=15, default='last name')
    phone = PhoneNumberField(default='+49 123 456 78')
    mobile = PhoneNumberField(default='+49 123 456 78')
    email = models.EmailField(max_length=254, default='me@domain.com')

    def __str__(self):
        return f"{self.reservation_date} | by {self.first_name}"


class TableReservation(models.Model):
    reservation_id = models.AutoField(primary_key=True)  # auto-increment ID

    time_slot = models.CharField(max_length=15, default='time_slot')
    number_of_tables_required_by_patron = models.IntegerField(default=0)
    reservation_status = models.BooleanField(default=True)
    booked_on_date = models.DateTimeField(auto_now=True)

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
        return f"Reservation {self.reservation_id} on {self.reservation_date}"


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


class OnlineRegisteredCustomer(models.Model):
    id = models.IntegerField(primary_key=True)
    first_name = models.CharField(max_length=15, default='first name')
    last_name = models.CharField(max_length=15, default='last name')
    phone = PhoneNumberField(default='+49 123 456 78')
    mobile = PhoneNumberField(default='+49 123 456 78')
    email = models.EmailField(max_length=254, default='me@domain.com')


class ReservedTables1718(models.Model):
    calendar_date = models.DateField(default=date.today, primary_key=True)
    timme_slot = models.CharField(max_length=15, default='17:00 to 18:00')

    table_1 = models.IntegerField(default=0)
    table_2 = models.IntegerField(default=0)
    table_3 = models.IntegerField(default=0)
    table_4 = models.IntegerField(default=0)
    table_5 = models.IntegerField(default=0)
    table_6 = models.IntegerField(default=0)
    table_7 = models.IntegerField(default=0)
    table_8 = models.IntegerField(default=0)
    table_9 = models.IntegerField(default=0)
    table_10 = models.IntegerField(default=0)


class ReservedTables1819(models.Model):
    calendar_date = models.DateField(default=date.today, primary_key=True)
    timme_slot = models.CharField(max_length=15, default='18:00 to 19:00')

    table_1 = models.IntegerField(default=0)
    table_2 = models.IntegerField(default=0)
    table_3 = models.IntegerField(default=0)
    table_4 = models.IntegerField(default=0)
    table_5 = models.IntegerField(default=0)
    table_6 = models.IntegerField(default=0)
    table_7 = models.IntegerField(default=0)
    table_8 = models.IntegerField(default=0)
    table_9 = models.IntegerField(default=0)
    table_10 = models.IntegerField(default=0)


class ReservedTables1920(models.Model):
    calendar_date = models.DateField(default=date.today, primary_key=True)
    timme_slot = models.CharField(max_length=15, default='19:00 to 20:00')

    table_1 = models.IntegerField(default=0)
    table_2 = models.IntegerField(default=0)
    table_3 = models.IntegerField(default=0)
    table_4 = models.IntegerField(default=0)
    table_5 = models.IntegerField(default=0)
    table_6 = models.IntegerField(default=0)
    table_7 = models.IntegerField(default=0)
    table_8 = models.IntegerField(default=0)
    table_9 = models.IntegerField(default=0)
    table_10 = models.IntegerField(default=0)


class ReservedTables2021(models.Model):
    calendar_date = models.DateField(default=date.today, primary_key=True)
    timme_slot = models.CharField(max_length=15, default='20:00 to 21:00')

    table_1 = models.IntegerField(default=0)
    table_2 = models.IntegerField(default=0)
    table_3 = models.IntegerField(default=0)
    table_4 = models.IntegerField(default=0)
    table_5 = models.IntegerField(default=0)
    table_6 = models.IntegerField(default=0)
    table_7 = models.IntegerField(default=0)
    table_8 = models.IntegerField(default=0)
    table_9 = models.IntegerField(default=0)
    table_10 = models.IntegerField(default=0)


class ReservedTables2122(models.Model):
    calendar_date = models.DateField(default=date.today, primary_key=True)
    timme_slot = models.CharField(max_length=15, default='21:00 to 22:00')

    table_1 = models.IntegerField(default=0)
    table_2 = models.IntegerField(default=0)
    table_3 = models.IntegerField(default=0)
    table_4 = models.IntegerField(default=0)
    table_5 = models.IntegerField(default=0)
    table_6 = models.IntegerField(default=0)
    table_7 = models.IntegerField(default=0)
    table_8 = models.IntegerField(default=0)
    table_9 = models.IntegerField(default=0)
    table_10 = models.IntegerField(default=0)


class BridgeEntity(models.Model):
    calendar_date = models.DateField(default=date.today, primary_key=True)
    date = models.DateField(default=date.today)


class Creditos1(models.Model):
    dict_info = JSONField(default=dict)
