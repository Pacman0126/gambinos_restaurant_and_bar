from datetime import timedelta, datetime
import logging
import re

from django.db.models import Q
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import login, get_user_model
# from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.mail import send_mail
from django.db.models import Q, Count, Sum
from django.template.loader import render_to_string
from django.http import JsonResponse
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.urls import reverse
from django.views.decorators.http import require_GET
from django.utils.crypto import get_random_string
from django.http import HttpResponseForbidden

from .models import TableReservation, TimeSlotAvailability, RestaurantConfig
from .forms import (
    SignUpForm,
    PhoneReservationForm,
    EditReservationForm,
)

User = get_user_model()

logger = logging.getLogger(__name__)


def _to_int(value, default=0):
    """
    Coerce None/blank to int default.
    """
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


# --- SLOT LABELS ---
SLOT_LABELS = {
    "17_18": "17:00–18:00",
    "18_19": "18:00–19:00",
    "19_20": "19:00–20:00",
    "20_21": "20:00–21:00",
    "21_22": "21:00–22:00",
}


def _build_next_30_days():
    """
    Build the same 30-day availability structure used by make_reservation,
    so staff phone-reservation can show exactly the same grid.

    Defensive against NULLs in DB by coercing everything with _to_int.
    """
    today = timezone.now().date()
    next_30_days = []

    # Get default tables from RestaurantConfig (fallback to 10 if not set)
    config = RestaurantConfig.objects.first()
    default_tables = config.default_tables_per_slot if config else 10

    for i in range(30):
        day = today + timedelta(days=i)

        # Ensure a TimeSlotAvailability row exists for this day
        ts, created = TimeSlotAvailability.objects.get_or_create(
            calendar_date=day,
            defaults={
                "number_of_tables_available_17_18": default_tables,
                "number_of_tables_available_18_19": default_tables,
                "number_of_tables_available_19_20": default_tables,
                "number_of_tables_available_20_21": default_tables,
                "number_of_tables_available_21_22": default_tables,
            },
        )

        slots = []
        for slot_key, label in SLOT_LABELS.items():
            demand_raw = getattr(
                ts, f"total_cust_demand_for_tables_{slot_key}", 0
            )
            available_raw = getattr(
                ts, f"number_of_tables_available_{slot_key}", default_tables
            )

            demand = _to_int(demand_raw, 0)
            available = _to_int(available_raw, default_tables)
            remaining = max(available - demand, 0)

            slots.append(
                {
                    "key": slot_key,
                    "label": label,
                    "demand": demand,
                    "available": available,
                    "remaining": remaining,
                }
            )

        ts.slots = slots
        next_30_days.append(ts)

    return next_30_days


def home(request):
    """Simple home view"""
    return render(request, "reservation_book/index.html")


@login_required
def make_reservation(request):
    logger.info("make_reservation called, method=%s", request.method)

    if request.method == "POST":
        try:
            # -----------------------------------
            # 1. Basic POST data
            # -----------------------------------
            date = request.POST.get("reservation_date")
            slot = request.POST.get("time_slot")

            is_ajax = request.headers.get(
                "x-requested-with") == "XMLHttpRequest"

            if not date or not slot:
                msg = "Please select a time slot before submitting."
                logger.warning(
                    "Reservation POST missing date or slot (date='%s', slot='%s')",
                    date,
                    slot,
                )
                if is_ajax:
                    return JsonResponse({"success": False, "error": msg})
                messages.error(request, msg)
                return redirect("make_reservation")

            tables_needed = int(
                request.POST.get("number_of_tables_required_by_patron", 1)
            )

            first_name = request.POST.get("first_name", "").strip()
            last_name = request.POST.get("last_name", "").strip()
            email = request.POST.get("email", "").strip()
            phone = request.POST.get("phone", "").strip()
            mobile = request.POST.get("mobile", "").strip()

            if not first_name or not last_name:
                msg = "First name and Last name are required."
                if is_ajax:
                    return JsonResponse({"success": False, "error": msg})
                messages.error(request, msg)
                return redirect("make_reservation")

            if not email:
                msg = "An email address is required so we can send confirmation."
                if is_ajax:
                    return JsonResponse({"success": False, "error": msg})
                messages.error(request, msg)
                return redirect("make_reservation")

            # -----------------------------------
            # 2. Availability lookup
            # -----------------------------------
            ts = TimeSlotAvailability.objects.get(calendar_date=date)
            slot_available = getattr(ts, f"number_of_tables_available_{slot}")
            slot_demand = getattr(ts, f"total_cust_demand_for_tables_{slot}")

            slot_available = _to_int(slot_available, 0)
            slot_demand = _to_int(slot_demand, 0)

            if slot_demand + tables_needed > slot_available:
                error_msg = "Not enough tables available."
                if is_ajax:
                    return JsonResponse({"success": False, "error": error_msg})
                messages.error(request, error_msg)
                return redirect("make_reservation")

            # -----------------------------------
            # 3. Decide which user this reservation belongs to
            # -----------------------------------

            user_for_reservation = None

            if request.user.is_authenticated and not request.user.is_staff:
                # Normal online user booking for themselves
                user_for_reservation = request.user

                # If they changed the email, optionally keep it in sync
                if email and email.lower() != request.user.email.lower():
                    request.user.email = email
                    request.user.save(update_fields=["email"])

                # Also update first/last name if they left them empty in their profile
                updated_fields = []
                if first_name and not request.user.first_name:
                    request.user.first_name = first_name
                    updated_fields.append("first_name")
                if last_name and not request.user.last_name:
                    request.user.last_name = last_name
                    updated_fields.append("last_name")
                if updated_fields:
                    request.user.save(update_fields=updated_fields)

            else:
                # Staff / phone reservation case:
                # If the email already exists, attach to that user.
                existing_user = User.objects.filter(
                    email__iexact=email
                ).first()

                if existing_user:
                    user_for_reservation = existing_user
                    logger.info(
                        "Phone reservation: linked to existing user id=%s, email=%s",
                        existing_user.id,
                        existing_user.email,
                    )
                else:
                    # Create a minimal account so this customer can log in later
                    base_username = email.split("@")[0] or "guest"
                    username = base_username
                    counter = 1
                    while User.objects.filter(username=username).exists():
                        username = f"{base_username}{counter}"
                        counter += 1

                    # You can refine password policy later
                    temp_password = User.objects.make_random_password()

                    user_for_reservation = User.objects.create_user(
                        username=username,
                        email=email,
                        password=temp_password,
                        first_name=first_name,
                        last_name=last_name,
                    )
                    logger.info(
                        "Phone reservation: created new user id=%s, username=%s, email=%s",
                        user_for_reservation.id,
                        user_for_reservation.username,
                        user_for_reservation.email,
                    )

            # -----------------------------------
            # 4. Create reservation
            # -----------------------------------
            reservation = TableReservation.objects.create(
                user=user_for_reservation,
                is_phone_reservation=request.user.is_staff,
                time_slot=slot,
                number_of_tables_required_by_patron=tables_needed,
                timeslot_availability=ts,
                reservation_status=True,
                reservation_date=ts.calendar_date,
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone=phone,
                mobile=mobile,
            )

            # Update demand
            setattr(
                ts,
                f"total_cust_demand_for_tables_{slot}",
                slot_demand + tables_needed,
            )
            ts.save()

            new_demand = getattr(ts, f"total_cust_demand_for_tables_{slot}")
            new_demand = _to_int(new_demand, 0)
            available_total = getattr(ts, f"number_of_tables_available_{slot}")
            available_total = _to_int(available_total, 0)
            left = available_total - new_demand
            pretty_slot = SLOT_LABELS.get(slot, slot)

            logger.info(
                "Reservation confirmed for %s %s at %s on %s (phone=%s, user_id=%s)",
                first_name,
                last_name,
                pretty_slot,
                date,
                request.user.is_staff,
                user_for_reservation.id if user_for_reservation else None,
            )

            # -----------------------------------
            # 5. Send confirmation email
            # -----------------------------------
            if email:
                try:
                    if request.user.is_staff:
                        # Staff / phone reservation template
                        template_name = (
                            "reservation_book/emails/phone_reservation_confirmation.txt"
                        )
                    else:
                        # Normal online reservation template (the one you had working before)
                        template_name = (
                            "reservation_book/emails/reservation_confirmation.txt"
                        )

                    message = render_to_string(
                        template_name,
                        {
                            "reservation": reservation,
                            "time_slot_pretty": pretty_slot,
                            "tables_needed": tables_needed,
                            "left": left,
                        },
                    )

                    send_mail(
                        subject="Your Gambinos reservation is confirmed",
                        message=message,
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[email],
                        fail_silently=False,
                    )
                    logger.info(
                        "Confirmation email sent to %s for reservation id=%s",
                        email,
                        reservation.id,
                    )
                except Exception as e:
                    logger.exception(
                        "Error sending reservation confirmation email: %s", e
                    )

            # -----------------------------------
            # 6. Response (AJAX or normal)
            # -----------------------------------
            if is_ajax:
                return JsonResponse(
                    {
                        "success": True,
                        "reservation_id": reservation.id,
                        "date": str(date),
                        "pretty_slot": pretty_slot,
                        "demand": new_demand,
                        "available": available_total,
                        "left": left,
                    }
                )

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
    # GET branch – same as before
    # ----------------
    next_30_days = _build_next_30_days()

    return render(
        request,
        "reservation_book/make_reservation.html",
        {
            "next_30_days": next_30_days,
            "slot_labels": SLOT_LABELS,
        },
    )


def signup(request):
    if request.method == "POST":
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)  # log in automatically after signup
            return redirect("make_reservation")  # redirect to reservation page
    else:
        form = SignUpForm()
    return render(request, "registration/signup.html", {"form": form})


@login_required
def cancel_reservation(request, reservation_id):
    reservation = get_object_or_404(TableReservation, id=reservation_id)

    # --- Permissions ---
    # Online user can always cancel their own reservation
    # Staff/owner can cancel any (incl. phone reservations)
    if reservation.user:
        if reservation.user != request.user and not request.user.is_staff:
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse(
                    {
                        "success": False,
                        "error": "You are not allowed to cancel this reservation.",
                    },
                    status=403,
                )
            messages.error(
                request, "You cannot cancel someone else's reservation.")
            return redirect("my_reservations")
    else:
        if not request.user.is_staff:
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse(
                    {
                        "success": False,
                        "error": "You are not allowed to cancel this reservation.",
                    },
                    status=403,
                )
            messages.error(
                request, "You are not allowed to cancel this reservation.")
            return redirect("my_reservations")

    # Already cancelled?
    if reservation.reservation_status is False:
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"success": True})
        messages.info(request, "This reservation has already been cancelled.")
        return redirect("my_reservations")

    # Snapshot OLD values
    old_date = reservation.reservation_date
    old_slot = reservation.time_slot
    old_tables = reservation.number_of_tables_required_by_patron

    # Mark as cancelled (this will also bump updated_at)
    reservation.reservation_status = False
    reservation.save()

    refreshed = TableReservation.objects.get(id=reservation_id)

    # --- Update demand: release tables from that date/slot ---
    try:
        ts = TimeSlotAvailability.DoesNotExist
        ts = TimeSlotAvailability.objects.get(calendar_date=old_date)
    except TimeSlotAvailability.DoesNotExist:
        ts = None

    if ts:
        demand_field = f"total_cust_demand_for_tables_{old_slot}"
        current_demand = getattr(ts, demand_field, 0) or 0
        new_demand = max(0, current_demand - old_tables)
        setattr(ts, demand_field, new_demand)
        ts.save()

    # --- Build & send cancellation email with timestamps ---
    recipient_email = None
    if refreshed.user and refreshed.user.email:
        recipient_email = refreshed.user.email
    elif getattr(refreshed, "email", None):
        recipient_email = refreshed.email

    if recipient_email:
        subject = "Your Gambinos reservation has been cancelled"

        def fmt_dt(dt):
            return dt.strftime("%b %d, %Y at %H:%M:%S")

        def fmt_day_slot(d, slot):
            try:
                day = d.strftime("%b %d, %Y")
            except Exception:
                day = str(d)
            return f"{day} at {SLOT_LABELS.get(slot, slot)}"

        def plural_s(n: int) -> str:
            return "" if n == 1 else "s"

        created_on = fmt_dt(refreshed.created_at)
        # updated_at reflects cancellation time
        cancelled_on = fmt_dt(refreshed.updated_at)
        when_str = fmt_day_slot(old_date, old_slot)

        # Name for greeting
        guest_name = ""
        if hasattr(refreshed, "name") and refreshed.name:
            guest_name = refreshed.name
        elif refreshed.user:
            guest_name = (
                refreshed.user.get_full_name() or refreshed.user.username
            )

        lines = []
        if guest_name:
            lines.append(f"Hello {guest_name},")
            lines.append("")

        lines.append(
            f"The reservation (created on {created_on})\n"
            f"for {old_tables} table{plural_s(old_tables)} on {when_str}\n"
            f"was cancelled on {cancelled_on}."
        )
        lines.append("")
        lines.append(f"Reservation ID: {refreshed.id}")
        if request.user.is_staff:
            lines.append(f"Cancelled by: STAFF ({request.user.username})")
        else:
            lines.append(f"Cancelled by: {request.user.username}")
        lines.append("")
        lines.append("Thank you for choosing Gambinos Restaurant & Lounge.")

        message = "\n".join(lines)

        send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL,
            [recipient_email],
            fail_silently=True,
        )

    # JSON vs redirect response
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"success": True})

    messages.success(request, "Your reservation has been cancelled.")
    return redirect("my_reservations")


@login_required
def my_reservations(request):
    reservations = (
        TableReservation.objects.filter(
            user=request.user, reservation_status=True)
        .order_by("timeslot_availability__calendar_date", "time_slot")
    )
    return render(
        request,
        "reservation_book/my_reservations.html",
        {"reservations": reservations},
    )


def menu(request):
    return render(request, "reservation_book/menu.html")


# -------------------------------------------------------------------
# Staff dashboard (cards for Phone Reservations, Customer Stats, etc.)
# -------------------------------------------------------------------
def staff_or_superuser_required(view_func):
    """
    Custom decorator: allow access if user is staff OR superuser.
    Also requires authenticated and active.
    """
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(settings.LOGIN_URL)
        if not request.user.is_active:
            return HttpResponseForbidden("Account inactive.")
        if request.user.is_superuser or request.user.is_staff:
            return view_func(request, *args, **kwargs)
        return HttpResponseForbidden("Staff access required.")
    return wrapper


@staff_or_superuser_required
def staff_dashboard(request):
    today = timezone.now().date()

    total_reservations = TableReservation.objects.count()
    upcoming_reservations_count = TableReservation.objects.filter(
        timeslot_availability__calendar_date__gte=today,
        reservation_status=True,
    ).count()
    phone_reservations_count = TableReservation.objects.filter(
        is_phone_reservation=True
    ).count()
    registered_customers_count = User.objects.exclude(
        reservations__isnull=True
    ).count()
    cancelled_reservations_count = TableReservation.objects.filter(
        reservation_status=False
    ).count()

    context = {
        "total_reservations": total_reservations,
        "upcoming_reservations_count": upcoming_reservations_count,
        "phone_reservations_count": phone_reservations_count,
        "registered_customers_count": registered_customers_count,
        "cancelled_reservations_count": cancelled_reservations_count,
    }
    return render(
        request,
        "reservation_book/staff_dashboard.html",
        context,
    )


@staff_or_superuser_required
def staff_reservations(request):
    """
    Staff view: search and manage ALL reservations (online + phone-in).

    Search by:
    - Booking ID (exact, if q is all digits)
    - username
    - email (user or phone-in email)
    - first/last name
    - phone or mobile
    """
    query = request.GET.get("q", "").strip()

    qs = (
        TableReservation.objects.select_related(
            "user", "timeslot_availability")
        .order_by(
            "-timeslot_availability__calendar_date",
            "-time_slot",
            "-created_at",
        )
    )

    if query:
        combined = Q()

        # If it's purely digits, treat as possible Booking ID
        if query.isdigit():
            combined |= Q(id=int(query))

        # Username / email (user or phone-in)
        combined |= Q(user__username__icontains=query)
        combined |= Q(user__email__icontains=query)
        combined |= Q(email__icontains=query)

        # First / last name
        combined |= Q(first_name__icontains=query)
        combined |= Q(last_name__icontains=query)

        # Phone fields that actually exist on your model
        combined |= Q(phone__icontains=query)
        combined |= Q(mobile__icontains=query)

        qs = qs.filter(combined)

    context = {
        "reservations": qs,
        "query": query,
    }
    return render(
        request,
        "reservation_book/staff_reservations.html",
        context,
    )


@staff_or_superuser_required
def user_reservations_overview(request):
    """
    Staff-facing overview of all customers who have at least one reservation.

    - total_reservations: all reservations (active + cancelled)
    - active_reservations: reservation_status=True (even if in the past)
    - cancelled_reservations: reservation_status=False
    - total_tables_booked: sum of tables across *all* reservations (history)
    - active_tables_booked: sum of tables for *upcoming* active reservations
      (reservation_status=True AND reservation_date >= today).
    """
    today = timezone.localdate()

    users = (
        User.objects.filter(reservations__isnull=False)
        .annotate(
            total_reservations=Count("reservations", distinct=True),
            active_reservations=Count(
                "reservations",
                filter=Q(reservations__reservation_status=True),
                distinct=True,
            ),
            cancelled_reservations=Count(
                "reservations",
                filter=Q(reservations__reservation_status=False),
                distinct=True,
            ),
            # lifetime total tables booked (all reservations)
            total_tables_booked=Coalesce(
                Sum("reservations__number_of_tables_required_by_patron"),
                0,
            ),
            # upcoming / today active tables
            active_tables_booked=Coalesce(
                Sum(
                    "reservations__number_of_tables_required_by_patron",
                    filter=Q(
                        reservations__reservation_status=True,
                        reservations__reservation_date__gte=today,
                    ),
                ),
                0,
            ),
        )
        .order_by("last_name", "first_name")
    )

    return render(
        request,
        "reservation_book/user_reservations_overview.html",
        {"users": users},
    )


@staff_or_superuser_required
def user_reservation_history(request, user_id):
    """
    Staff view: full reservation history for a given registered customer.
    """
    history_user = get_object_or_404(User, id=user_id)
    reservations = (
        TableReservation.objects.filter(user=history_user)
        .select_related("timeslot_availability")
        .order_by(
            "-timeslot_availability__calendar_date",
            "-time_slot",
        )
    )

    return render(
        request,
        "reservation_book/user_reservation_history.html",
        {
            "history_user": history_user,
            "reservations": reservations,
        },
    )


@staff_or_superuser_required
def create_phone_reservation(request):
    """
    Staff UI for creating reservations for phone-in customers.

    - Shows the same 30-day availability grid as make_reservation.
    - Email field is used to:
        * Link to an existing user if the email matches.
        * Otherwise create a new User with that email so future bookings
          are tied to the same account.
    - Sends a reservation confirmation email.
    - For newly created users, also includes a 'set your password' link
      so they can access their account later.
    """
    # Build the same availability grid used by make_reservation
    next_30_days = _build_next_30_days()

    selected_date = None

    if request.method == "POST":
        # Try to parse date early so PhoneReservationForm can validate
        raw_date = request.POST.get("reservation_date")
        if raw_date:
            try:
                selected_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                selected_date = None

        form = PhoneReservationForm(request.POST, for_date=selected_date)

        if form.is_valid():
            cd = form.cleaned_data
            reservation_date = cd["reservation_date"]
            time_slot = cd["time_slot"]
            tables_needed = cd["number_of_tables_required_by_patron"]
            first_name = cd["first_name"]
            last_name = cd["last_name"]
            phone = cd["phone"]
            mobile = cd["mobile"]
            # should be present, but stay defensive
            email = cd.get("email") or None

            # Get or create availability row for the chosen date
            timeslot_avail, _ = TimeSlotAvailability.objects.get_or_create(
                calendar_date=reservation_date
            )

            avail_field = f"number_of_tables_available_{time_slot}"
            demand_field = f"total_cust_demand_for_tables_{time_slot}"

            slot_available = getattr(timeslot_avail, avail_field, 0) or 0
            slot_demand = getattr(timeslot_avail, demand_field, 0) or 0
            remaining = slot_available - slot_demand

            if tables_needed > remaining:
                messages.error(
                    request,
                    f"Not enough tables available for "
                    f"{SLOT_LABELS.get(time_slot, time_slot)} "
                    f"on {reservation_date}. "
                    f"Remaining: {max(remaining, 0)}.",
                )
                return render(
                    request,
                    "reservation_book/create_phone_reservation.html",
                    {
                        "form": form,
                        "slot_labels": SLOT_LABELS,
                        "remaining_for_slot": max(remaining, 0),
                        "next_30_days": next_30_days,
                    },
                )

            # --- Link to existing online customer by email, or create one ---
            linked_user = None
            new_user_created = False

            if email:
                try:
                    existing_user = User.objects.get(email__iexact=email)
                    linked_user = existing_user
                except User.DoesNotExist:
                    existing_user = None

                if existing_user is None:
                    # Create a new User record so this email now has an account
                    base_username = email.split("@")[0]
                    candidate_username = slugify(
                        f"{first_name}-{last_name}-{base_username}"
                    )[:150] or base_username[:150]

                    # Ensure uniqueness of username
                    original = candidate_username
                    counter = 1
                    while User.objects.filter(username=candidate_username).exists():
                        candidate_username = f"{original}-{counter}"
                        counter += 1

                    linked_user = User.objects.create_user(
                        username=candidate_username,
                        email=email,
                        first_name=first_name,
                        last_name=last_name,
                        password=User.objects.make_random_password(),
                    )
                    new_user_created = True

            # --- Create the reservation ---
            reservation = TableReservation.objects.create(
                user=linked_user,
                reservation_date=reservation_date,
                time_slot=time_slot,
                number_of_tables_required_by_patron=tables_needed,
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                mobile=mobile,
                email=email,
                is_phone_reservation=True,
                timeslot_availability=timeslot_avail,
            )

            # Update cumulative demand for that slot
            setattr(
                timeslot_avail,
                demand_field,
                slot_demand + tables_needed,
            )
            timeslot_avail.save()

            # --- Email: confirmation + (for new users) account setup link ---
            if email:
                context = {
                    "reservation": reservation,
                    "slot_label": SLOT_LABELS.get(time_slot, time_slot),
                }

                subject = "Your Gambinos reservation (phone booking)"
                message = render_to_string(
                    "reservation_book/emails/phone_reservation_confirmation.txt",
                    context,
                )

                # If we just created a user, give them a password-reset link
                # so they can set their password and log in later.
                if new_user_created:
                    reset_url = (
                        request.build_absolute_uri(
                            reverse("account_reset_password")
                        )
                        + f"?email={email}"
                    )
                    message += (
                        "\n\nWe’ve set up an online account for you with this email."
                        "\nTo set your password and manage your bookings online, "
                        f"visit: {reset_url}"
                    )

                send_mail(
                    subject,
                    message,
                    settings.DEFAULT_FROM_EMAIL,
                    [email],
                    fail_silently=True,
                )

            pretty_slot = SLOT_LABELS.get(time_slot, time_slot)
            messages.success(
                request,
                f"Phone reservation created for {first_name} {last_name} on "
                f"{reservation_date} at {pretty_slot}.",
            )

            # After creating a phone reservation, redirect to staff overview
            return redirect("user_reservations_overview")

    else:
        form = PhoneReservationForm()

    return render(
        request,
        "reservation_book/create_phone_reservation.html",
        {
            "form": form,
            "slot_labels": SLOT_LABELS,
            "next_30_days": next_30_days,
        },
    )


@login_required
def update_reservation(request, reservation_id):
    reservation = get_object_or_404(TableReservation, id=reservation_id)
    session_key = f"reservation_edit_snapshot_{reservation_id}"

    # --- Permissions ---
    if reservation.user:
        # Online booking: only that user OR staff can update
        if reservation.user != request.user and not request.user.is_staff:
            messages.error(
                request, "You are not allowed to update this reservation."
            )
            return redirect("my_reservations")
    else:
        # Phone reservation (no linked user): only staff/owner can update
        if not request.user.is_staff:
            messages.error(
                request, "You are not allowed to update this phone reservation."
            )
            return redirect("my_reservations")

    if request.method == "POST":
        form = EditReservationForm(request.POST, instance=reservation)

        if form.is_valid():
            # --- OLD values: from snapshot stored when Edit page was opened ---
            snap = request.session.get(session_key, {})

            def parse_date(val, fallback):
                if not val:
                    return fallback
                try:
                    return datetime.strptime(val, "%Y-%m-%d").date()
                except ValueError:
                    return fallback

            def parse_int(val, fallback):
                try:
                    return int(val)
                except (TypeError, ValueError):
                    return fallback

            old_date = parse_date(
                snap.get("date"),
                reservation.reservation_date,
            )
            old_slot = snap.get("slot", reservation.time_slot)
            old_tables = parse_int(
                snap.get("tables"),
                reservation.number_of_tables_required_by_patron,
            )

            # --- NEW values: read directly from POST ---
            date_str = request.POST.get("reservation_date")
            new_date = parse_date(date_str, old_date)

            new_slot = request.POST.get("time_slot") or old_slot

            tables_str = request.POST.get(
                "number_of_tables_required_by_patron")
            new_tables = parse_int(tables_str, old_tables)

            # --- Release OLD demand from TimeSlotAvailability ---
            old_ts = None
            old_demand_field = None
            old_demand_value = None

            try:
                old_ts = TimeSlotAvailability.objects.get(
                    calendar_date=old_date)
            except TimeSlotAvailability.DoesNotExist:
                old_ts = None

            if old_ts:
                old_demand_field = f"total_cust_demand_for_tables_{old_slot}"
                old_demand_value = getattr(old_ts, old_demand_field, 0) or 0
                setattr(
                    old_ts,
                    old_demand_field,
                    max(0, old_demand_value - old_tables),
                )
                old_ts.save()

            # --- Check NEW slot availability ---
            new_ts, _ = TimeSlotAvailability.objects.get_or_create(
                calendar_date=new_date
            )

            avail_field = f"number_of_tables_available_{new_slot}"
            demand_field = f"total_cust_demand_for_tables_{new_slot}"

            slot_available = getattr(new_ts, avail_field, 0) or 0
            slot_demand = getattr(new_ts, demand_field, 0) or 0
            remaining = slot_available - slot_demand

            if new_tables > remaining:
                # Not enough capacity in the new slot
                messages.error(
                    request,
                    f"Not enough tables available for "
                    f"{SLOT_LABELS.get(new_slot, new_slot)} on {new_date}. "
                    f"Remaining: {max(remaining, 0)}.",
                )

                # roll back OLD demand if we changed it
                if (
                    old_ts
                    and old_demand_field is not None
                    and old_demand_value is not None
                ):
                    setattr(old_ts, old_demand_field, old_demand_value)
                    old_ts.save()

                return render(
                    request,
                    "reservation_book/edit_reservation.html",
                    {"form": form, "reservation": reservation},
                )

            # --- Commit NEW demand ---
            setattr(new_ts, demand_field, slot_demand + new_tables)
            new_ts.save()

            # --- Save reservation with NEW values (including other form fields) ---
            new_reservation = form.save(commit=False)
            new_reservation.reservation_date = new_date
            new_reservation.time_slot = new_slot
            new_reservation.number_of_tables_required_by_patron = new_tables
            new_reservation.timeslot_availability = new_ts
            new_reservation.save()

            # Snapshot no longer needed
            if session_key in request.session:
                del request.session[session_key]

            refreshed = TableReservation.objects.get(id=reservation_id)

            # --- Build & send detailed UPDATE email with timestamps ---
            recipient_email = None
            if refreshed.user and refreshed.user.email:
                recipient_email = refreshed.user.email
            elif getattr(refreshed, "email", None):
                recipient_email = refreshed.email

            # Name for greeting
            guest_name = ""
            if hasattr(refreshed, "name") and refreshed.name:
                guest_name = refreshed.name
            elif refreshed.user:
                guest_name = (
                    refreshed.user.get_full_name() or refreshed.user.username
                )

            if recipient_email:
                subject = "Your Gambinos reservation has been updated"

                def fmt_dt(dt):
                    return dt.strftime("%b %d, %Y at %H:%M:%S")

                def fmt_day_slot(d, slot):
                    try:
                        day = d.strftime("%b %d, %Y")
                    except Exception:
                        day = str(d)
                    return f"{day} at {SLOT_LABELS.get(slot, slot)}"

                def plural_s(n: int) -> str:
                    return "" if n == 1 else "s"

                created_on = fmt_dt(refreshed.created_at)
                updated_on = fmt_dt(refreshed.updated_at)
                old_when = fmt_day_slot(old_date, old_slot)
                new_when = fmt_day_slot(new_date, new_slot)

                lines = []
                if guest_name:
                    lines.append(f"Hello {guest_name},")
                    lines.append("")

                lines.append(
                    f"The reservation (created on {created_on})\n"
                    f"for {old_tables} table{plural_s(old_tables)} on {old_when}\n"
                    f"was updated on {updated_on}\n"
                    f"to {new_tables} table{plural_s(new_tables)} on {new_when}."
                )
                lines.append("")
                lines.append(f"Reservation ID: {refreshed.id}")
                if request.user.is_staff:
                    lines.append(
                        f"Updated by: STAFF ({request.user.username})")
                else:
                    lines.append(f"Updated by: {request.user.username}")
                lines.append("")
                lines.append(
                    "Thank you for choosing Gambinos Restaurant & Lounge."
                )

                message = "\n".join(lines)

                send_mail(
                    subject,
                    message,
                    settings.DEFAULT_FROM_EMAIL,
                    [recipient_email],
                    fail_silently=True,
                )

            messages.success(request, "Your reservation has been updated.")
            return redirect("my_reservations")

    else:
        # GET: take a snapshot of what the user sees *before* editing
        if reservation.reservation_date:
            date_str = reservation.reservation_date.strftime("%Y-%m-%d")
        else:
            date_str = ""

        request.session[session_key] = {
            "date": date_str,
            "slot": reservation.time_slot,
            "tables": str(
                reservation.number_of_tables_required_by_patron
            ),
        }

        form = EditReservationForm(instance=reservation)

    current_slot_label = SLOT_LABELS.get(
        reservation.time_slot, reservation.time_slot
    )

    return render(
        request,
        "reservation_book/edit_reservation.html",
        {
            "form": form,
            "reservation": reservation,
            "current_slot_label": current_slot_label,
        },
    )


def _normalize_query(q: str) -> str:
    """
    - strips
    - collapses whitespace
    - removes whitespace around '@' in emails ("name @gmail.com" -> "name@gmail.com")
    """
    q = (q or "").strip()
    q = re.sub(r"\s+", " ", q)
    q = re.sub(r"\s*@\s*", "@", q)
    return q


@login_required
@require_GET
def ajax_lookup_customer(request):
    if not request.user.is_staff:
        return JsonResponse({"results": []}, status=403)

    raw_query = request.GET.get("q", "").strip()
    mode = request.GET.get("mode", "past").lower()

    q = _normalize_query(raw_query)

    if len(q) < 2:
        return JsonResponse({"results": []})

    User = get_user_model()
    today = timezone.now().date()
    results = []

    # ------------------- ALWAYS CHECK FOR RESERVATION ID -------------------
    stripped_q = q.strip()
    is_numeric_id = stripped_q.isdigit()
    reservation_by_id = None
    if is_numeric_id:
        try:
            res_by_id = TableReservation.objects.filter(
                id=int(stripped_q)).first()
            if res_by_id:
                # Build the same dict format as before
                date_val = res_by_id.reservation_date or (
                    res_by_id.timeslot_availability.calendar_date if res_by_id.timeslot_availability else None
                )
                reservation_by_id = {
                    "type": "reservation",
                    "reservation_id": res_by_id.id,
                    "first_name": res_by_id.first_name or "",
                    "last_name": res_by_id.last_name or "",
                    "email": res_by_id.email or "",
                    "phone": res_by_id.phone or "",
                    "mobile": res_by_id.mobile or "",
                    "reservation_date": date_val.isoformat() if date_val else "",
                    "time_slot": res_by_id.time_slot or "",
                    "pretty_slot": SLOT_LABELS.get(res_by_id.time_slot, res_by_id.time_slot or ""),
                    "reservation_status": bool(getattr(res_by_id, "reservation_status", True)),
                }
        except (ValueError, OverflowError):
            pass  # Not a valid int

    # If we found a reservation by exact ID, add it immediately (prioritize it)
    if reservation_by_id:
        results.append(reservation_by_id)

        # If mode is past and we have an ID match, still return it (staff might need to edit)
        # But continue to customer search below if needed

    # ------------------------------------------------------------------
    # Mode: existing → active/upcoming reservations (text + ID already added above)
    # ------------------------------------------------------------------
    if mode == "existing":
        res_filter = (
            Q(email__iexact=q)
            | Q(email__icontains=q)
            | Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
        )

        reservations_qs = (
            TableReservation.objects
            .select_related("timeslot_availability")
            .filter(res_filter)
            .filter(reservation_status=True, reservation_date__gte=today)
            .order_by("-reservation_date", "-created_at")[:10]
        )

        for r in reservations_qs:
            # Skip if already added as exact ID match
            if reservation_by_id and r.id == reservation_by_id["reservation_id"]:
                continue

            date_val = r.reservation_date or (
                r.timeslot_availability.calendar_date if r.timeslot_availability else None
            )

            results.append({
                "type": "reservation",
                "reservation_id": r.id,
                "first_name": r.first_name or "",
                "last_name": r.last_name or "",
                "email": r.email or "",
                "phone": r.phone or "",
                "mobile": r.mobile or "",
                "reservation_date": date_val.isoformat() if date_val else "",
                "time_slot": r.time_slot or "",
                "pretty_slot": SLOT_LABELS.get(r.time_slot, r.time_slot or ""),
                "reservation_status": True,
            })

        return JsonResponse({"results": results})

    # ------------------------------------------------------------------
    # Mode: past (default) → customer profiles
    # ------------------------------------------------------------------
    # (If ID match already added above, it will appear first)

    user_filter = (
        Q(email__iexact=q)
        | Q(email__icontains=q)
        | Q(username__iexact=q)
        | Q(username__icontains=q)
        | Q(first_name__icontains=q)
        | Q(last_name__icontains=q)
    )

    users_qs = User.objects.filter(user_filter).order_by(
        "last_name", "first_name")[:10]

    customer_results = []
    seen_emails = set()

    for u in users_qs:
        email_lower = (u.email or "").lower()
        if email_lower:
            seen_emails.add(email_lower)

        customer_results.append({
            "type": "customer",
            "user_id": u.id,
            "first_name": u.first_name or "",
            "last_name": u.last_name or "",
            "email": u.email or "",
            "phone": "",
            "mobile": "",
        })

    past_filter = (
        Q(email__iexact=q)
        | Q(email__icontains=q)
        | Q(first_name__icontains=q)
        | Q(last_name__icontains=q)
    )

    past_qs = TableReservation.objects.filter(
        past_filter).order_by("-created_at")[:25]

    for r in past_qs:
        em = (r.email or "").strip().lower()
        if em and em in seen_emails:
            continue
        if em:
            seen_emails.add(em)

        customer_results.append({
            "type": "customer",
            "user_id": None,
            "first_name": r.first_name or "",
            "last_name": r.last_name or "",
            "email": r.email or "",
            "phone": r.phone or "",
            "mobile": r.mobile or "",
        })

    results.extend(customer_results)

    return JsonResponse({"results": results})


def superuser_required(view_func):
    """Decorator: only allow superusers"""
    decorated_view_func = user_passes_test(
        lambda u: u.is_superuser,
        login_url='staff_dashboard'  # or wherever you want to redirect
    )(view_func)
    return decorated_view_func


@superuser_required
def staff_management(request):
    staff_users = User.objects.filter(
        is_staff=True).order_by('last_name', 'first_name')
    return render(request, 'reservation_book/staff_management.html', {
        'staff_users': staff_users
    })


@superuser_required
def add_staff(request):
    if request.method == 'POST':
        first_name = request.POST.get('first_name').strip()
        last_name = request.POST.get('last_name').strip()
        email = request.POST.get('email').strip().lower()

        if not all([first_name, last_name, email]):
            messages.error(request, "All fields are required.")
            return redirect('staff_management')

        if User.objects.filter(email=email).exists():
            messages.error(request, "A user with this email already exists.")
            return redirect('staff_management')

        # Generate temporary password
        temp_password = get_random_string(length=12)

        # Create user
        user = User.objects.create_user(
            username=email,  # Use email as username (common & secure)
            email=email,
            first_name=first_name,
            last_name=last_name,
            password=temp_password,
            is_staff=True,
            is_active=True
        )

        # Send email
        # Adjust if you have custom login URL
        login_url = request.build_absolute_uri(reverse('login'))
        subject = "Welcome to Gambino's Restaurant - Staff Account Created"
        message = f"""
        Hello {first_name},

        Your staff account has been created for Gambino's Restaurant & Bar.

        Please log in using the details below and change your password immediately:

        Login URL: {login_url}
        Username: {email}
        Temporary Password: {temp_password}

        For security, please change your password after logging in.

        Thank you,
        Management
        """

        # try:
        #     send_mail(
        #         subject=subject,
        #         message=message,
        #         from_email=settings.DEFAULT_FROM_EMAIL,
        #         recipient_list=[email],
        #         fail_silently=False,
        #     )
        #     messages.success(
        #         request, f"Staff member {first_name} {last_name} added and email sent.")
        # except Exception as e:
        #     messages.warning(
        #         request, f"Staff added but email failed to send: {str(e)}")
        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email],
                fail_silently=False,  # This will raise if send fails
            )
            messages.success(
                request, f"Staff member {first_name} {last_name} added and email sent."
            )
            logger.info(f"Staff welcome email sent to {email}")
        except Exception as e:
            logger.error(f"Email send failed for {email}: {str(e)}")
            messages.warning(
                request,
                f"Staff member added, but email failed to send: {str(e)}"
            )
        return redirect('staff_management')

    return redirect('staff_management')  # GET → just go back


@superuser_required
def remove_staff(request, user_id):
    user = get_object_or_404(User, id=user_id)

    if user.is_superuser:
        messages.error(request, "Cannot remove a superuser.")
    elif user == request.user:
        messages.error(request, "You cannot remove yourself.")
    else:
        user.is_staff = False
        user.is_active = False  # Optional: deactivate login
        user.save()
        messages.success(
            request, f"Staff access removed for {user.get_full_name() or user.email}.")

    return redirect('staff_management')
