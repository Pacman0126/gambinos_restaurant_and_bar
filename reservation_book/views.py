import logging
from datetime import date, timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.utils import timezone
from django.core.mail import send_mail
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required

from .forms import ReservationForm
from .models import ReservationBook, TableReservation, TimeSlotAvailability

logger = logging.getLogger(__name__)

# --- SLOT LABELS ---
SLOT_LABELS = {
    "17_18": "17:00–18:00",
    "18_19": "18:00–19:00",
    "19_20": "19:00–20:00",
    "20_21": "20:00–21:00",
    "21_22": "21:00–22:00",
}


def home(request):
    """Simple home view"""
    return render(request, "reservation_book/index.html")


# def reservations(request):
#     """
#     Display an individual :model:`reservation_book.ReservationBook`.

#     **Context**

#     ``post``
#         An instance of :model:`reservation_book.ReservationBook`.

#     **Template:**

#     :template:`reservation_book/index.html`
#     """

#     reservations = ReservationBook.objects.all()
#     # reservation = get_object_or_404(queryset, reservation_id=reservation_id)

#     return render(
#         request,
#         "reservation_book/reservation_book.html",
#         {"reservations": reservations}
#     )


def make_reservation(request):
    logger.info("make_reservation called, method=%s", request.method)

    if request.method == "POST":
        try:
            date = request.POST.get("reservation_date")
            slot = request.POST.get("time_slot")
            tables_needed = int(request.POST.get(
                "number_of_tables_required_by_patron", 1))

            first_name = request.POST.get("first_name")
            last_name = request.POST.get("last_name")
            phone = request.POST.get("phone")
            mobile = request.POST.get("mobile")
            email = request.POST.get("email")

            is_ajax = request.headers.get(
                "x-requested-with") == "XMLHttpRequest"

            # Find availability
            ts = TimeSlotAvailability.objects.get(calendar_date=date)
            slot_available = getattr(ts, f"number_of_tables_available_{slot}")
            slot_demand = getattr(ts, f"total_cust_demand_for_tables_{slot}")

            # Check capacity
            if slot_demand + tables_needed > slot_available:
                if is_ajax:
                    return JsonResponse({"success": False, "error": "Not enough tables available"})
                messages.error(request, "Not enough tables available.")
                return redirect("make_reservation")

            # Require contact info
            if not (phone or mobile):
                if is_ajax:
                    return JsonResponse({"success": False, "error": "Phone or mobile required"})
                messages.error(
                    request, "Please provide at least a phone or mobile number.")
                return redirect("make_reservation")

            # Save reservation
            reservation = TableReservation.objects.create(
                time_slot=slot,
                number_of_tables_required_by_patron=tables_needed,
                timeslot_availability=ts,
                reservation_status=True,
            )

            demand_field = f"total_cust_demand_for_tables_{slot}"
            setattr(ts, demand_field, getattr(
                ts, demand_field) + tables_needed)
            ts.save()

            left = slot_available - getattr(ts, demand_field)

            # Log confirmation
            pretty_slot = SLOT_LABELS.get(slot, slot)
            logger.info(
                f"Reservation confirmed for {first_name} {last_name} at {pretty_slot} on {date}")

            if is_ajax:
                return JsonResponse({"success": True, "left": left})

            messages.success(request, "Reservation confirmed!")
            return redirect("make_reservation")
        except Exception as e:
            logger.exception(
                "Unexpected error in make_reservation POST: %s", e)
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"success": False, "error": str(e)})
            messages.error(request, f"Error processing reservation: {e}")
            return redirect("make_reservation")

    # ----------------
    # GET branch
    # ----------------
    today = timezone.now().date()
    next_30_days = []

    for i in range(30):
        day = today + timedelta(days=i)

        # Ensure a TimeSlotAvailability row exists for this day
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

        # Calculate remaining tables per slot
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
