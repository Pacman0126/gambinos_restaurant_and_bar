from django.db import models
from django.contrib.auth.models import User
from phonenumber_field.modelfields import PhoneNumberField
from phonenumber_field.phonenumber import PhoneNumber
from datetime import datetime
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


class TableReservation(models.Model):
    reservation_id = models.IntegerField(primary_key=True)
    reservation_id = models.OneToOneField(
        ReservationBook,
        on_delete=models.CASCADE,
        primary_key=True,
        unique=True
    )
