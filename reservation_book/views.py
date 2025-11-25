import logging
from datetime import timedelta, datetime

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.db.models import Q, Count, Sum
from django.template.loader import render_to_string
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.urls import reverse
from urllib.parse import urlencode

from .models import TableReservation, TimeSlotAvailability
from .forms import SignUpForm, PhoneReservationForm, EditReservationForm

User = get_user_model()
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


@login_required
def make_reservation(request):
    logger.info("make_reservation called, method=%s", request.method)

    if request.method == "POST":
        try:
            date = request.POST.get("reservation_date")
            slot = request.POST.get("time_slot")

            is_ajax = request.headers.get(
                "x-requested-with") == "XMLHttpRequest"

            if not date or not slot:
                msg = "Please select a time slot before submitting."
                if is_ajax:
                    return JsonResponse({"success": False, "error": msg})
                messages.error(request, msg)
                return redirect("make_reservation")

            tables_needed = int(request.POST.get(
                "number_of_tables_required_by_patron", 1))

            first_name = request.POST.get("first_name", "").strip()
            last_name = request.POST.get("last_name", "").strip()

            if not first_name or not last_name:
                msg = "First name and Last name are required."
                if is_ajax:
                    return JsonResponse({"success": False, "error": msg})
                messages.error(request, msg)
                return redirect("make_reservation")

            phone = request.POST.get("phone", "").strip()
            mobile = request.POST.get("mobile", "").strip()

            # --- Check availability ---
            ts = TimeSlotAvailability.objects.get(calendar_date=date)
            slot_available = getattr(ts, f"number_of_tables_available_{slot}")
            slot_demand = getattr(ts, f"total_cust_demand_for_tables_{slot}")

            if slot_demand + tables_needed > slot_available:
                error_msg = "Not enough tables available."
                if is_ajax:
                    return JsonResponse({"success": False, "error": error_msg})
                messages.error(request, error_msg)
                return redirect("make_reservation")

            # --- Save reservation ---
            reservation = TableReservation.objects.create(
                user=request.user,   # tie to logged-in user
                time_slot=slot,
                number_of_tables_required_by_patron=tables_needed,
                timeslot_availability=ts,
                reservation_status=True,
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                mobile=mobile,
            )

            # --- Update demand ---
            setattr(
                ts,
                f"total_cust_demand_for_tables_{slot}",
                slot_demand + tables_needed
            )
            ts.save()

            # --- Prepare counts ---
            new_demand = getattr(ts, f"total_cust_demand_for_tables_{slot}")
            available_total = getattr(ts, f"number_of_tables_available_{slot}")
            left = available_total - new_demand
            pretty_slot = SLOT_LABELS.get(slot, slot)

            logger.info(
                f"Reservation confirmed for {first_name} {last_name} at {pretty_slot} on {date}"
            )

            if is_ajax:
                return JsonResponse({
                    "success": True,
                    "reservation_id": reservation.id,
                    "date": str(date),
                    "pretty_slot": pretty_slot,
                    "demand": new_demand,        # send updated demand
                    "available": available_total,  # total available
                    "left": left                  # remaining
                })

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

    # Get default tables from RestaurantConfig (fallback to 10 if not set)
    from .models import RestaurantConfig
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
            demand = getattr(ts, f"total_cust_demand_for_tables_{slot_key}")
            available = getattr(ts, f"number_of_tables_available_{slot_key}")
            remaining = max(available - demand, 0)  # precompute remaining

            slots.append({
                "key": slot_key,
                "label": label,
                "demand": demand,
                "available": available,
                "remaining": remaining,
            })

        ts.slots = slots
        next_30_days.append(ts)

    return render(request, "reservation_book/make_reservation.html", {
        "next_30_days": next_30_days,
        "slot_labels": SLOT_LABELS,
    })


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
                refreshed.user.get_full_name()
                or refreshed.user.username
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
    reservations = TableReservation.objects.filter(
        user=request.user, reservation_status=True
    ).order_by("timeslot_availability__calendar_date", "time_slot")
    return render(request, "reservation_book/my_reservations.html", {
        "reservations": reservations
    })


def menu(request):
    return render(request, "reservation_book/menu.html")


User = get_user_model()


@staff_member_required
def user_reservations_overview(request):
    """
    Staff dashboard: per-customer reservation stats.
    Only includes users who have at least one reservation.
    """
    users = (
        User.objects.annotate(
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
            total_tables_booked=Sum(
                "reservations__number_of_tables_required_by_patron"
            ),
        )
        .filter(total_reservations__gt=0)
        .order_by("-total_reservations", "email")
    )

    return render(
        request,
        "reservation_book/user_reservations_overview.html",
        {"users": users},
    )


@staff_member_required
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


@staff_member_required
def create_phone_reservation(request):
    """
    Staff UI for creating reservations for phone-in customers.

    - Optionally links to an existing User if the email matches.
    - If email is new, saves it on the reservation and sends:
        * a reservation confirmation
        * an optional 'complete your account' link.
    - Enforces table availability based on TimeSlotAvailability.
    """

    selected_date = None
    if request.method == "POST":
        # Try to parse date early so PhoneReservationForm can show availability
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
            email = cd.get("email") or None

            # Get or create availability row
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
                    f"Not enough tables available for {SLOT_LABELS.get(time_slot, time_slot)} "
                    f"on {reservation_date}. Remaining: {max(remaining, 0)}."
                )
                return render(
                    request,
                    "reservation_book/create_phone_reservation.html",
                    {
                        "form": form,
                        "slot_labels": SLOT_LABELS,
                        "remaining_for_slot": max(remaining, 0),
                    },
                )

            # --- Link to existing online customer by email ---
            linked_user = None
            existing_user = None

            if email:
                try:
                    existing_user = User.objects.get(email__iexact=email)
                except User.DoesNotExist:
                    existing_user = None

            if existing_user:
                # Always attach reservation to the existing user object
                linked_user = existing_user
            else:
                # No user yet — will store raw email, customer can register later
                linked_user = None

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

            # Update cumulative demand
            setattr(
                timeslot_avail,
                demand_field,
                slot_demand + tables_needed,
            )
            timeslot_avail.save()

            # --- Email handling ---
            if email:
                # Use your existing confirmation template if you have one.
                context = {
                    "reservation": reservation,
                    "slot_label": SLOT_LABELS.get(time_slot, time_slot),
                }

                subject = "Your Gambinos reservation (phone booking)"
                message = render_to_string(
                    "reservation_book/emails/phone_reservation_confirmation.txt",
                    context,
                )

                # Optionally add a 'complete your account' link for new customers
                if not linked_user:
                    signup_url = request.build_absolute_uri(
                        reverse("account_signup")
                    ) + f"?email={email}"
                    message += (
                        "\n\nDon’t have an online account yet?\n"
                        f"Create one here to manage your bookings: {signup_url}"
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
        {"form": form, "slot_labels": SLOT_LABELS},
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
                request, "You are not allowed to update this reservation.")
            return redirect("my_reservations")
    else:
        # Phone reservation (no linked user): only staff/owner can update
        if not request.user.is_staff:
            messages.error(
                request, "You are not allowed to update this phone reservation.")
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
                if old_ts and old_demand_field is not None and old_demand_value is not None:
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
                    refreshed.user.get_full_name()
                    or refreshed.user.username
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
                    "Thank you for choosing Gambinos Restaurant & Lounge.")

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
            "tables": str(reservation.number_of_tables_required_by_patron),
        }

        form = EditReservationForm(instance=reservation)

    current_slot_label = SLOT_LABELS.get(
        reservation.time_slot, reservation.time_slot)

    return render(
        request,
        "reservation_book/edit_reservation.html",
        {
            "form": form,
            "reservation": reservation,
            "current_slot_label": current_slot_label,
        },
    )
