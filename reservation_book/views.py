from django.shortcuts import render, HttpResponse, redirect
from django.contrib import messages
from django.utils import timezone
from django.core.mail import send_mail

from twilio.rest import Client

from .forms import ReservationForm
from datetime import date, timedelta
from django.http import JsonResponse

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


def send_sms(to_number, body):
    """
    Helper to send SMS using Twilio. Credentials come from environment variables or settings.
    """
    client = Client(
        os.environ.get("TWILIO_ACCOUNT_SID", getattr(
            settings, "TWILIO_ACCOUNT_SID", "")),
        os.environ.get("TWILIO_AUTH_TOKEN", getattr(
            settings, "TWILIO_AUTH_TOKEN", "")),
    )
    client.messages.create(
        body=body,
        from_=os.environ.get("TWILIO_PHONE_NUMBER", getattr(
            settings, "TWILIO_PHONE_NUMBER", "+441234567890")),
        to=to_number,
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

        # detect if AJAX request
        is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

        try:
            ts = TimeSlotAvailability.objects.get(calendar_date=date)
        except TimeSlotAvailability.DoesNotExist:
            if is_ajax:
                return JsonResponse({"success": False, "error": "Invalid date"})
            messages.error(request, "Invalid date selected.")
            return redirect("make_reservation")

        slot_available = getattr(ts, f"number_of_tables_available_{slot}")
        slot_demand = getattr(ts, f"total_cust_demand_for_tables_{slot}")

        # check availability
        if slot_demand + tables_needed > slot_available:
            if is_ajax:
                return JsonResponse({"success": False, "error": "Not enough tables available"})
            messages.error(request, "Not enough tables available.")
            return redirect("make_reservation")

        # must provide at least one phone number
        if not (phone or mobile):
            if is_ajax:
                return JsonResponse({"success": False, "error": "Phone or mobile required"})
            messages.error(
                request, "Please provide at least a phone or mobile number.")
            return redirect("make_reservation")

        # create reservation
        reservation = TableReservation.objects.create(
            time_slot=slot,
            number_of_tables_required_by_patron=tables_needed,
            timeslot_availability=ts,
            reservation_status=True,
        )

        # update demand for that slot
        demand_field = f"total_cust_demand_for_tables_{slot}"
        setattr(ts, demand_field, getattr(ts, demand_field) + tables_needed)
        ts.save()

        # compute remaining availability (✅ only once)
        left = slot_available - getattr(ts, demand_field)

        # send confirmation email
        if email:
            send_mail(
                subject="Your Gambino’s Reservation Confirmation",
                message=f"Hello {first_name}, your reservation on {date} at {slot.replace('_', ':')} is confirmed.",
                from_email="no-reply@gambinos.com",
                recipient_list=[email],
                fail_silently=False,
            )

        # send SMS if mobile provided
        if mobile:
            send_sms(
                to_number=mobile,
                body=f"Hello {first_name}, your reservation on {date} at {slot.replace('_', ':')} is confirmed."
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
                id=reservation.pk,
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                mobile=mobile,
                email=email,
            )

        # ✅ AJAX JSON response here
        if is_ajax:
            return JsonResponse({"success": True, "left": left})

        # fallback: classic redirect
        messages.success(request, "Reservation confirmed!")
        return redirect("make_reservation")

    # -------------------
    # GET branch (outside POST)
    # -------------------
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
        ts.slots = slots
        next_30_days.append(ts)

    return render(request, "reservation_book/make_reservation.html", {
        "next_30_days": next_30_days,
    })


def reservation_success(request):
    return render(request, "reservation_book/reservation_success.html")


def reservation_list(request):
    reservations = ReservationBook.objects.select_related(
        "reservation_id").order_by("reservation_date")
    return render(request, "reservation_book/reservation_list.html", {
        "reservations": reservations
    })
