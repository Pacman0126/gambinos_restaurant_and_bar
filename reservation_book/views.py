from django.shortcuts import render, HttpResponse, redirect
from django.contrib import messages
from django.utils import timezone
from .forms import ReservationForm
from datetime import date, timedelta


from .models import TableReservation, TimeSlotAvailability, OnlineRegisteredCustomer, ReservationBook
from django.contrib.auth.decorators import login_required
# import pandas as pd
# import sqlalchemy
import json

# from django.views import generic
from .models import ReservationBook

from .models import TableReservation, TimeSlotAvailability
# from .models import Creditos1

# from .forms import ReservationsForm


# from django.http import HttpResponse


# Create your views here.
# https://stackoverflow.com/questions/68248414/how-to-store-a-dictionary-in-a-django-database-models-field

def importExcel(request):
    today = date.today()
    created_rows = []

    for i in range(30):
        day = today + timedelta(days=i)

        # Create or get existing record
        obj, created = TimeSlotAvailability.objects.get_or_create(
            calendar_date=day,
            defaults={
                "total_cust_demand_for_tables_17_18": 0,
                "number_of_tables_available_17_18": 10,
                "total_cust_demand_for_tables_18_19": 0,
                "number_of_tables_available_18_19": 10,
                "total_cust_demand_for_tables_19_20": 0,
                "number_of_tables_available_19_20": 10,
                "total_cust_demand_for_tables_20_21": 0,
                "number_of_tables_available_20_21": 10,
                "total_cust_demand_for_tables_21_22": 0,
                "number_of_tables_available_21_22": 10,
            },
        )
        created_rows.append(obj)

    return render(
        request,
        "reservation_book/reservation_book.html",
        {"table_availability": created_rows},
    )


def home(request):
    """
    Display an individual :model:`reservation_book.ReservationBook`.

    **Context**

    ``post``
        An instance of :model:`reservation_book.ReservationBook`.

    **Template:**

    :template:`reservation_book/index.html`
    """

    reservations = ReservationBook.objects.all()
    # reservation = get_object_or_404(queryset, reservation_id=reservation_id, )

    return render(
        request,
        "reservation_book/index.html",
        {"reservations": reservations}
    )


def reservations(request):
    """
    Display an individual :model:`reservation_book.ReservationBook`.

    **Context**

    ``post``
        An instance of :model:`reservation_book.ReservationBook`.

    **Template:**

    :template:`reservation_book/index.html`
    """

    reservations = ReservationBook.objects.all()
    # reservation = get_object_or_404(queryset, reservation_id=reservation_id)

    return render(
        request,
        "reservation_book/reservation_book.html",
        {"reservations": reservations}
    )


def make_reservation(request):
    if request.method == "POST":
        date = request.POST.get("reservation_date")
        slot = request.POST.get("time_slot")
        tables_needed = int(request.POST.get(
            "number_of_tables_required_by_patron", 1))

        first_name = request.POST.get("first_name")
        last_name = request.POST.get("last_name")
        phone = request.POST.get("phone")
        mobile = request.POST.get("mobile")
        email = request.POST.get("email")

        try:
            ts = TimeSlotAvailability.objects.get(calendar_date=date)
        except TimeSlotAvailability.DoesNotExist:
            messages.error(request, "Invalid date selected.")
            return redirect("make_reservation")

        # check availability
        slot_available = getattr(ts, f"number_of_tables_available_{slot}")
        slot_demand = getattr(ts, f"total_cust_demand_for_tables_{slot}")

        if slot_demand + tables_needed > slot_available:
            messages.error(request, "Not enough tables available.")
            return redirect("make_reservation")

        # save TableReservation
        reservation = TableReservation.objects.create(
            time_slot=slot,
            number_of_tables_required_by_patron=tables_needed,
            timeslot_availability=ts,
            reservation_status=True,
        )

        # link to Online or Staff
        if request.user.is_authenticated and request.user.is_staff:
            ReservationBook.objects.create(
                reservation_id=request.user,
                reservation_date=date,
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                mobile=mobile,
                email=email,
            )
        else:
            OnlineRegisteredCustomer.objects.create(
                id=reservation.pk,  # link to reservation
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                mobile=mobile,
                email=email,
            )

        messages.success(request, "Reservation confirmed!")
        return redirect("make_reservation")

    # -----------------------
    # GET branch → ensure 30-day availability
    # -----------------------
    today = timezone.now().date()
    next_30_days = []

    for i in range(30):
        day = today + timedelta(days=i)
        ts, created = TimeSlotAvailability.objects.get_or_create(
            calendar_date=day,
            defaults={
                "number_of_tables_available_17_18": 10,
                "number_of_tables_available_18_19": 10,
                "number_of_tables_available_19_20": 10,
                "number_of_tables_available_20_21": 10,
                "number_of_tables_available_21_22": 10,
            },
        )

        slots = [
            ("17_18", ts.number_of_tables_available_17_18 -
             ts.total_cust_demand_for_tables_17_18),
            ("18_19", ts.number_of_tables_available_18_19 -
             ts.total_cust_demand_for_tables_18_19),
            ("19_20", ts.number_of_tables_available_19_20 -
             ts.total_cust_demand_for_tables_19_20),
            ("20_21", ts.number_of_tables_available_20_21 -
             ts.total_cust_demand_for_tables_20_21),
            ("21_22", ts.number_of_tables_available_21_22 -
             ts.total_cust_demand_for_tables_21_22),
        ]
        ts.slots = slots  # attach for template use
        next_30_days.append(ts)

    return render(request, "reservation_book/make_reservation.html", {
        "next_30_days": next_30_days,
    })


def reservation_success(request):
    return render(request, "reservation_book/reservation_success.html")
