from django.shortcuts import redirect, render
from django.core.mail import send_mail
from django.contrib.auth.decorators import login_required
from datetime import timedelta
from datetime import timedelta, datetime
import logging
import re

import datetime
from django.urls import reverse
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.contrib.auth.tokens import default_token_generator
from django.conf import settings
from django.contrib import messages
# from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import login, get_user_model, authenticate
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import SetPasswordForm
# from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required, user_passes_test


from django.core.mail import send_mail, EmailMultiAlternatives
from django.db.models import Q, Count, Sum
from django.template.loader import render_to_string
from django.http import JsonResponse
from django.db.models.functions import Coalesce
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify

from django.views.decorators.http import require_GET, require_http_methods
from django.utils.crypto import get_random_string
from django.http import HttpResponseForbidden
from allauth.account.models import EmailAddress

# from .decorators import staff_or_superuser_required
from .constants import SLOT_LABELS
from .models import SLOT_KEYS
from .models import TimeSlotAvailability, RestaurantConfig, Customer, ReservationSeries
from .forms import (
    SignUpForm,
    PhoneReservationForm,
    EditReservationForm,
)


SLOT_KEYS = list(SLOT_LABELS.keys())


def _normalize_email(email: str) -> str:
    return ((email or "").strip().lower())


def menu(request):
    return render(request, "reservation_book/menu.html")


def _customer_for_logged_in_user(request):
    if not request.user.is_authenticated:
        return None

    email = _normalize_email(getattr(request.user, "email", ""))
    if not email:
        return None

    # If we normalize to a canonical form, we can query by exact match.
    return Customer.objects.filter(email=email).first()


def _default_tables_per_slot() -> int:
    config = RestaurantConfig.objects.first()
    return int(getattr(config, "default_tables_per_slot", 20) or 20)


def _timeslot_defaults() -> dict:
    default_cap = _default_tables_per_slot()
    d = {}
    for key in SLOT_KEYS:
        d[f"number_of_tables_available_{key}"] = default_cap
        d[f"total_cust_demand_for_tables_{key}"] = 0
    return d


def _update_ts_demand(ts, slots, tables_needed: int, delta_sign: int):
    """
    delta_sign: +1 to add demand, -1 to subtract demand
    """
    if tables_needed <= 0 or not slots:
        return

    update_fields = []
    for s in slots:
        field = f"total_cust_demand_for_tables_{s}"
        current = _to_int(getattr(ts, field, 0), 0)
        new_val = max(current + (delta_sign * tables_needed), 0)
        setattr(ts, field, new_val)
        update_fields.append(field)

    ts.save(update_fields=update_fields)


def _capacity_ok(ts, slots, tables_needed: int):
    """
    Ensure for each slot:
        demand + tables_needed <= available
    """
    for s in slots:
        avail_field = f"number_of_tables_available_{s}"
        demand_field = f"total_cust_demand_for_tables_{s}"
        available = _to_int(getattr(ts, avail_field, 0), 0)
        demand = _to_int(getattr(ts, demand_field, 0), 0)
        if demand + tables_needed > available:
            return False, s, available, demand
    return True, None, None, None


def superuser_required(view_func):
    """Decorator to restrict view to superusers only"""
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            return HttpResponseForbidden("You do not have permission to access this page.")
        return view_func(request, *args, **kwargs)
    return wrapper


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


def _safe_int(val, fallback=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return fallback

# --- SLOT LABELS --- moved to constants.py


def _slot_order():
    """Return time-slot keys in chronological order."""
    try:
        return list(SLOT_LABELS.keys())
    except Exception:
        # Fallback: keep a stable common order
        return ["17_18", "18_19", "19_20", "20_21", "21_22"]


def _pretty_range_from_start_and_duration(start_slot: str, duration_hours: int) -> str:
    """
    Converts ('17_18', 4) => '17:00–21:00'
    Falls back safely to the single slot label.
    """
    slots = _slot_order()
    if start_slot not in slots:
        return SLOT_LABELS.get(start_slot, start_slot)

    duration_hours = int(duration_hours or 1)
    duration_hours = max(1, duration_hours)

    start_idx = slots.index(start_slot)
    end_idx = min(start_idx + duration_hours - 1, len(slots) - 1)

    first_label = SLOT_LABELS.get(
        slots[start_idx], slots[start_idx])  # "17:00–18:00"
    last_label = SLOT_LABELS.get(
        slots[end_idx], slots[end_idx])      # "20:00–21:00" for 4h

    try:
        start_t = first_label.split("–")[0].strip()
        end_t = last_label.split("–")[1].strip()
        return f"{start_t}–{end_t}"
    except Exception:
        return first_label


def _affected_slots(start_slot: str, duration: int, until_close: bool = False):
    slots = _slot_order()
    if start_slot not in slots:
        return []
    start_index = slots.index(start_slot)
    if until_close:
        return slots[start_index:]
    end_index = min(start_index + max(int(duration or 1), 1), len(slots))
    return slots[start_index:end_index]


def _cancel_and_release(reservation: TableReservation) -> None:
    """
    Cancel a reservation and RELEASE table demand back into TimeSlotAvailability.

    Handles:
    - Single-hour reservations
    - Multi-hour blocks via duration_hours (releases demand across consecutive slot keys)
    - Safe atomic update (locks the TimeSlotAvailability row)
    - Never deletes Customer/User data; only cancels and updates availability

    IMPORTANT:
    If your model has BOTH:
        - legacy boolean: reservation_status
        - new lifecycle field: status
    we update BOTH so admin + reporting stay correct.
    """

    # ----------------------------
    # Idempotent: already cancelled?
    # ----------------------------
    # New system
    if hasattr(reservation, "status"):
        if reservation.status == getattr(TableReservation, "STATUS_CANCELLED", "cancelled"):
            return

    # Legacy system
    if hasattr(reservation, "reservation_status"):
        if reservation.reservation_status is False:
            # Still also ensure status is cancelled if the field exists
            if hasattr(reservation, "status") and reservation.status != getattr(TableReservation, "STATUS_CANCELLED", "cancelled"):
                reservation.status = getattr(
                    TableReservation, "STATUS_CANCELLED", "cancelled")
                if hasattr(reservation, "cancelled_at"):
                    reservation.cancelled_at = timezone.now()
                    reservation.save(update_fields=["status", "cancelled_at"])
                else:
                    reservation.save(update_fields=["status"])
            return

    # ----------------------------
    # Determine slot key ordering
    # ----------------------------
    # Prefer SLOT_KEYS if it exists; otherwise fall back to SLOT_LABELS.keys()
    try:
        slot_keys = list(SLOT_KEYS)  # uses your global ordering if present
    except Exception:
        labels = globals().get("SLOT_LABELS", {}) or {}
        try:
            slot_keys = list(labels.keys())
        except Exception:
            slot_keys = []

    start_slot = getattr(reservation, "time_slot", None)

    try:
        tables_needed = int(
            getattr(reservation, "number_of_tables_required_by_patron", 0) or 0)
    except Exception:
        tables_needed = 0

    try:
        duration = int(getattr(reservation, "duration_hours", 1) or 1)
    except Exception:
        duration = 1
    if duration < 1:
        duration = 1

    affected_slots = []
    if start_slot and start_slot in slot_keys:
        start_index = slot_keys.index(start_slot)
        affected_slots = slot_keys[start_index: start_index + duration]
    elif start_slot:
        affected_slots = [start_slot]

    # ----------------------------
    # Apply cancellation + release
    # ----------------------------
    with transaction.atomic():
        # 1) Mark reservation cancelled (NEW + legacy)
        update_fields = []

        if hasattr(reservation, "mark_cancelled"):
            # Your model helper should set: status + cancelled_at + reservation_status (if exists)
            reservation.mark_cancelled()

            # Build update_fields safely
            if hasattr(reservation, "status"):
                update_fields.append("status")
            if hasattr(reservation, "cancelled_at"):
                update_fields.append("cancelled_at")
            if hasattr(reservation, "reservation_status"):
                update_fields.append("reservation_status")
        else:
            # Fallback if helper not present
            if hasattr(reservation, "status"):
                reservation.status = getattr(
                    TableReservation, "STATUS_CANCELLED", "cancelled")
                update_fields.append("status")
            if hasattr(reservation, "cancelled_at"):
                reservation.cancelled_at = timezone.now()
                update_fields.append("cancelled_at")
            if hasattr(reservation, "reservation_status"):
                reservation.reservation_status = False
                update_fields.append("reservation_status")

        if update_fields:
            reservation.save(update_fields=update_fields)
        else:
            reservation.save()

        # 2) Locate/lock TSA row
        ts = getattr(reservation, "timeslot_availability", None)

        # If reservation doesn't have TSA linked (should be rare), fall back to date-based TSA
        if ts is None and getattr(reservation, "reservation_date", None):
            ts, _ = TimeSlotAvailability.objects.get_or_create(
                calendar_date=reservation.reservation_date,
                defaults=_timeslot_defaults(),  # assumes your helper exists
            )

        if ts is None:
            return

        ts = TimeSlotAvailability.objects.select_for_update().get(pk=ts.pk)

        # 3) Release demand across all affected slots
        tsa_update_fields = []
        if affected_slots and tables_needed > 0:
            for slot in affected_slots:
                if not slot:
                    continue

                demand_field = f"total_cust_demand_for_tables_{slot}"

                # Guard: only touch fields that actually exist on the TSA model
                if not hasattr(ts, demand_field):
                    continue

                current = int(getattr(ts, demand_field, 0) or 0)
                new_val = max(0, current - tables_needed)
                setattr(ts, demand_field, new_val)
                tsa_update_fields.append(demand_field)

        if tsa_update_fields:
            ts.save(update_fields=tsa_update_fields)


def _reservation_contact_email(reservation: TableReservation) -> str | None:
    """
    Best email to contact the guest for this reservation.

    Since TableReservation has no `user` FK, we use:
    - reservation.customer.email (preferred)
    - fallback: None
    """
    cust = getattr(reservation, "customer", None)
    email = (getattr(cust, "email", "") or "").strip()
    return email or None


def _reservation_owner_email(reservation) -> str:
    """
    TableReservation has no `user` FK in this project.
    Ownership is by email: request.user.email <-> reservation.customer.email
    """
    c = getattr(reservation, "customer", None)
    return ((getattr(c, "email", "") or "").strip().lower())


def _request_user_email(request) -> str:
    return ((getattr(request.user, "email", "") or "").strip().lower())


def _attach_time_range_pretty(reservations):
    """
    Adds r.time_range_pretty = '17:00–21:00' for templates.
    Uses your existing helper _pretty_range_from_start_and_duration.
    """
    for r in reservations:
        try:
            r.time_range_pretty = _pretty_range_from_start_and_duration(
                r.time_slot,
                getattr(r, "duration_hours", 1) or 1,
            )
        except Exception:
            # Safe fallback
            r.time_range_pretty = SLOT_LABELS.get(r.time_slot, r.time_slot)
    return reservations


@login_required
def _reservation_edit_allowed(request, reservation: TableReservation) -> bool:
    """
    Permissions for editing a reservation.

    IMPORTANT:
    TableReservation has NO `user` FK in your model.

    Rules:
    - Staff/superuser can edit anything.
    - Otherwise the logged-in user may edit ONLY if:
        request.user.email matches reservation.customer.email
      (via your Customer model).
    """
    # Staff can always edit
    if request.user.is_staff or request.user.is_superuser:
        return True

    # Must be logged in (decorator enforces, but keep safe)
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return False

    # Must have an email to match
    user_email = (getattr(request.user, "email", "") or "").strip().lower()
    if not user_email:
        return False

    # Reservation must have a customer with an email
    cust = getattr(reservation, "customer", None)
    cust_email = (getattr(cust, "email", "") or "").strip().lower()

    return bool(cust_email and cust_email == user_email)


def _reservation_contact_name(
    reservation: TableReservation,
    fallback_user=None,
) -> str:
    """
    Best display name for the guest.

    Since TableReservation has no `user` FK, we use:
    - reservation.customer.first_name / last_name (preferred)
    - fallback_user.get_full_name() or fallback_user.username (if provided)
    - "" if nothing is available
    """
    cust = getattr(reservation, "customer", None)
    first = (getattr(cust, "first_name", "") or "").strip()
    last = (getattr(cust, "last_name", "") or "").strip()
    full = (f"{first} {last}").strip()
    if full:
        return full

    if fallback_user is not None:
        try:
            name = (fallback_user.get_full_name() or "").strip()
        except Exception:
            name = ""
        if name:
            return name
        return (getattr(fallback_user, "username", "") or "").strip()

    return ""


@login_required
def cancel_reservation(request, reservation_id):
    """
    Cancel a reservation (no deletes), and release demand back to availability.

    IMPORTANT:
    TableReservation has NO `user` FK. Permissions via email match:
      request.user.email -> Customer.email -> reservation.customer
    """
    reservation = get_object_or_404(TableReservation, id=reservation_id)
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    if not _reservation_edit_allowed(request, reservation):
        msg = "You cannot cancel this reservation."
        if is_ajax:
            return JsonResponse({"success": False, "error": msg}, status=403)
        messages.error(request, msg)
        return redirect("my_reservations")

    # Idempotent: if already cancelled, treat as success
    if reservation.reservation_status is False:
        if is_ajax:
            return JsonResponse({"success": True})
        messages.info(request, "This reservation has already been cancelled.")
        return redirect("my_reservations")

    # Uses your atomic helper that releases demand across duration_hours slots
    _cancel_and_release(reservation)

    # -------- Optional: send cancellation email --------
    refreshed = TableReservation.objects.get(id=reservation_id)
    recipient_email = _reservation_contact_email(refreshed)
    guest_name = _reservation_contact_name(
        refreshed, fallback_user=request.user)

    if recipient_email:
        subject = "Your Gambinos reservation has been cancelled"

        def fmt_day_slot(d, slot):
            try:
                day = d.strftime("%b %d, %Y")
            except Exception:
                day = str(d)
            return f"{day} at {SLOT_LABELS.get(slot, slot)}"

        def plural_s(n: int) -> str:
            return "" if n == 1 else "s"

        when_str = fmt_day_slot(
            refreshed.reservation_date, refreshed.time_slot)
        tables = int(
            getattr(refreshed, "number_of_tables_required_by_patron", 0) or 0)

        lines = []
        if guest_name:
            lines.append(f"Hello {guest_name},")
            lines.append("")

        lines.append(
            f"Your reservation for {tables} table{plural_s(tables)} on {when_str} "
            f"has been cancelled."
        )
        lines.append("")
        lines.append(f"Reservation ID: {refreshed.id}")
        if request.user.is_staff:
            lines.append(f"Cancelled by: STAFF ({request.user.username})")
        else:
            lines.append(f"Cancelled by: {request.user.username}")
        lines.append("")
        lines.append("Thank you for choosing Gambinos Restaurant & Lounge.")

        send_mail(
            subject=subject,
            message="\n".join(lines),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient_email],
            fail_silently=True,
        )

    if is_ajax:
        return JsonResponse({"success": True})

    messages.success(request, "Reservation cancelled.")
    return redirect("my_reservations")


@login_required
def update_reservation(request, reservation_id):
    """
    Customer edit reservation.

    IMPORTANT:
    TableReservation has NO `user` FK. Permissions are enforced via:
      request.user.email -> Customer.email -> reservation.customer

    Notes:
    - Availability math is updated using _apply_reservation_change() which handles duration_hours blocks.
    - We do NOT delete Customer/User on edit.
    """
    reservation = get_object_or_404(TableReservation, id=reservation_id)
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    # ---------- Permissions ----------
    if not _reservation_edit_allowed(request, reservation):
        msg = "You are not allowed to edit this reservation."
        if is_ajax:
            return JsonResponse({"success": False, "error": msg}, status=403)
        messages.error(request, msg)
        return redirect("my_reservations")

    # ---------- Edit ----------
    if request.method == "POST":
        form = EditReservationForm(request.POST, instance=reservation)

        if not form.is_valid():
            if is_ajax:
                return JsonResponse(
                    {"success": False, "error": "Please correct the errors."},
                    status=400,
                )
            messages.error(request, "Please correct the errors below.")
            return render(
                request,
                "reservation_book/edit_reservation.html",
                {
                    "form": form,
                    "reservation": reservation,
                    "slot_labels": SLOT_LABELS,
                },
            )

        # OLD snapshot (for email text only)
        old_date = reservation.reservation_date
        old_slot = reservation.time_slot
        old_tables = reservation.number_of_tables_required_by_patron
        old_duration = getattr(reservation, "duration_hours", 1) or 1

        # NEW values (from form)
        new_date = form.cleaned_data.get("reservation_date")
        new_slot = form.cleaned_data.get("time_slot")
        new_tables = form.cleaned_data.get(
            "number_of_tables_required_by_patron")

        # Duration is not currently editable in EditReservationForm,
        # so preserve the existing stored duration_hours.
        new_duration = old_duration

        # Apply change + save reservation + update demand in ONE place.
        # If there are extra editable fields on the form, push them
        # onto the instance BEFORE applying the change.
        #
        # (If the EditReservationForm only includes date/slot/tables,
        # this still works and avoids double-saving.)

        # Pull any other form fields onto the instance without saving yet
        reservation = form.save(commit=False)

        try:
            _apply_reservation_change(
                reservation,
                new_date=new_date,
                new_start_slot=new_slot,
                new_duration=new_duration,
                new_tables_needed=new_tables,
            )
        except ValueError as e:
            # _apply_reservation_change already rolls back demand safely
            msg = str(e)
            if is_ajax:
                return JsonResponse({"success": False, "error": msg}, status=400)
            messages.error(request, msg)
            return render(
                request,
                "reservation_book/edit_reservation.html",
                {
                    "form": form,
                    "reservation": reservation,
                    "slot_labels": SLOT_LABELS,
                },
            )

        # Save any other editable fields from the form (if any)
        form.save()

        # -------- Optional: send update email --------
        refreshed = TableReservation.objects.get(id=reservation_id)
        recipient_email = _reservation_contact_email(refreshed)
        guest_name = _reservation_contact_name(
            refreshed, fallback_user=request.user)

        if recipient_email:
            subject = "Your Gambinos reservation has been updated"

            def fmt_day_slot(d, slot):
                try:
                    day = d.strftime("%b %d, %Y")
                except Exception:
                    day = str(d)
                return f"{day} at {SLOT_LABELS.get(slot, slot)}"

            def plural_s(n: int) -> str:
                return "" if n == 1 else "s"

            old_when = fmt_day_slot(old_date, old_slot)
            new_when = fmt_day_slot(new_date, new_slot)

            lines = []
            if guest_name:
                lines.append(f"Hello {guest_name},")
                lines.append("")

            lines.append(
                f"Your reservation for {old_tables} table{plural_s(int(old_tables or 0))} on {old_when}\n"
                f"was updated to {new_tables} table{plural_s(int(new_tables or 0))} on {new_when}."
            )
            lines.append("")
            lines.append(f"Reservation ID: {refreshed.id}")
            if request.user.is_staff:
                lines.append(f"Updated by: STAFF ({request.user.username})")
            else:
                lines.append(f"Updated by: {request.user.username}")
            lines.append("")
            lines.append(
                "Thank you for choosing Gambinos Restaurant & Lounge.")

            send_mail(
                subject=subject,
                message="\n".join(lines),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient_email],
                fail_silently=True,
            )

        if is_ajax:
            return JsonResponse({"success": True})

        messages.success(request, "Your reservation has been updated.")
        return redirect("my_reservations")

    # GET
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
            "slot_labels": SLOT_LABELS,
        },
    )


@transaction.atomic
def _apply_reservation_change(reservation, *, new_date, new_start_slot, new_duration, new_tables_needed):
    old_ts = TimeSlotAvailability.objects.select_for_update().get(
        pk=reservation.timeslot_availability_id)

    # resolve / create new day record
    new_ts, _ = TimeSlotAvailability.objects.get_or_create(
        calendar_date=new_date,
        defaults=_timeslot_defaults(),
    )
    new_ts = TimeSlotAvailability.objects.select_for_update().get(pk=new_ts.pk)

    old_slots = _affected_slots(
        reservation.time_slot, reservation.duration_hours or 1, until_close=False)
    old_tables = _to_int(reservation.number_of_tables_required_by_patron, 0)

    new_slots = _affected_slots(
        new_start_slot, new_duration or 1, until_close=False)
    new_tables = _to_int(new_tables_needed, 0)

    # 1) release old demand (only if active)
    if reservation.reservation_status:
        _update_ts_demand(old_ts, old_slots, old_tables, delta_sign=-1)

    # 2) capacity check on new TS
    ok, bad_slot, avail, demand = _capacity_ok(new_ts, new_slots, new_tables)
    if not ok:
        # put old demand back (since we released it above)
        if reservation.reservation_status:
            _update_ts_demand(old_ts, old_slots, old_tables, delta_sign=+1)
        raise ValueError(
            f"Not enough tables for {new_date} slot {SLOT_LABELS.get(bad_slot, bad_slot)}.")

    # 3) save new reservation values
    reservation.reservation_date = new_date
    reservation.timeslot_availability = new_ts
    reservation.time_slot = new_start_slot
    reservation.duration_hours = new_duration
    reservation.number_of_tables_required_by_patron = new_tables
    reservation.save()

    # 4) consume new demand
    if reservation.reservation_status:
        _update_ts_demand(new_ts, new_slots, new_tables, delta_sign=+1)


def _build_next_30_days():
    today = timezone.localdate()
    days = []

    for offset in range(30):
        date = today + timedelta(days=offset)

        ts, _ = TimeSlotAvailability.objects.get_or_create(
            calendar_date=date,
            defaults=_timeslot_defaults(),
        )

        slots = []
        default_cap = _default_tables_per_slot()

        for key in SLOT_KEYS:
            available = _safe_int(
                getattr(ts, f"number_of_tables_available_{key}", None),
                default_cap,
            )
            demand = _safe_int(
                getattr(ts, f"total_cust_demand_for_tables_{key}", None),
                0,
            )

            slots.append({
                "key": key,
                "label": SLOT_LABELS[key],
                "available": available,
                "demand": demand,
                "remaining": max(available - demand, 0),
            })

        # ✅ IMPORTANT: match what templates render: ts.calendar_date and ts.slots
        days.append({
            "calendar_date": ts.calendar_date,  # or `date`
            "slots": slots,
            # keep these if anything else relies on them (harmless):
            "date": date,
            "timeslot": ts,
        })

    return days


def home(request):
    """Simple home view"""
    return render(request, "reservation_book/index.html")


# @login_required
# def make_reservation(request):
#     logger.info("make_reservation called, method=%s", request.method)
#     manage_url = request.build_absolute_uri(reverse("my_reservations"))

#     if request.method == "POST":
#         try:
#             # -----------------------------------
#             # 1. Basic POST data
#             # -----------------------------------
#             date = request.POST.get("reservation_date")
#             slot = request.POST.get("time_slot")

#             is_ajax = request.headers.get(
#                 "x-requested-with") == "XMLHttpRequest"

#             if not date or not slot:
#                 msg = "Please select a time slot before submitting."
#                 logger.warning(
#                     "Reservation POST missing date or slot (date='%s', slot='%s')",
#                     date,
#                     slot,
#                 )
#                 if is_ajax:
#                     return JsonResponse({"success": False, "error": msg})
#                 messages.error(request, msg)
#                 return redirect("make_reservation")

#             tables_needed = int(
#                 request.POST.get("number_of_tables_required_by_patron", 1)
#             )

#             first_name = request.POST.get("first_name", "").strip()
#             last_name = request.POST.get("last_name", "").strip()
#             email = _normalize_email(request.POST.get("email", ""))
#             phone = request.POST.get("phone", "").strip()
#             mobile = request.POST.get("mobile", "").strip()

#             if not first_name or not last_name:
#                 msg = "First name and Last name are required."
#                 if is_ajax:
#                     return JsonResponse({"success": False, "error": msg})
#                 messages.error(request, msg)
#                 return redirect("make_reservation")

#             if not email:
#                 msg = "An email address is required so we can send confirmation."
#                 if is_ajax:
#                     return JsonResponse({"success": False, "error": msg})
#                 messages.error(request, msg)
#                 return redirect("make_reservation")

#             # -----------------------------------
#             # 2. Availability lookup
#             # -----------------------------------
#             ts = TimeSlotAvailability.objects.get(calendar_date=date)
#             slot_available = getattr(ts, f"number_of_tables_available_{slot}")
#             slot_demand = getattr(ts, f"total_cust_demand_for_tables_{slot}")

#             slot_available = _to_int(slot_available, 0)
#             slot_demand = _to_int(slot_demand, 0)

#             if slot_demand + tables_needed > slot_available:
#                 error_msg = "Not enough tables available."
#                 if is_ajax:
#                     return JsonResponse({"success": False, "error": error_msg})
#                 messages.error(request, error_msg)
#                 return redirect("make_reservation")

#             # -----------------------------------
#             # 3. Get or create Customer
#             # -----------------------------------
#             customer_data = {
#                 'first_name': first_name,
#                 'last_name': last_name,
#                 'email': email,
#                 'phone': phone,
#                 'mobile': mobile,
#             }

#             customer, created = Customer.objects.get_or_create(
#                 email=email,
#                 defaults=customer_data
#             )
#             if not created:
#                 # Update existing customer
#                 for key, value in customer_data.items():
#                     setattr(customer, key, value)
#                 customer.save()

#             # -----------------------------------
#             # 4. Decide which user this reservation belongs to (for authenticated users)
#             # -----------------------------------
#             user_for_reservation = None
#             if request.user.is_authenticated and not request.user.is_staff:
#                 user_for_reservation = request.user

#                 # Optionally sync user details to customer
#                 updated = False
#                 if first_name and not user_for_reservation.first_name:
#                     user_for_reservation.first_name = first_name
#                     updated = True
#                 if last_name and not user_for_reservation.last_name:
#                     user_for_reservation.last_name = last_name
#                     updated = True
#                 if email.lower() != user_for_reservation.email.lower():
#                     user_for_reservation.email = email
#                     updated = True
#                 if updated:
#                     user_for_reservation.save()

#             # -----------------------------------
#             # 5. Create reservation
#             # -----------------------------------
#             reservation = TableReservation.objects.create(
#                 is_phone_reservation=request.user.is_staff,
#                 time_slot=slot,
#                 number_of_tables_required_by_patron=tables_needed,
#                 timeslot_availability=ts,
#                 reservation_status=True,
#                 reservation_date=ts.calendar_date,
#                 customer=customer,  # Link to the Customer object we created/updated
#                 user=user_for_reservation,
#             )

#             # Update demand
#             setattr(
#                 ts,
#                 f"total_cust_demand_for_tables_{slot}",
#                 slot_demand + tables_needed,
#             )
#             ts.save()

#             new_demand = getattr(ts, f"total_cust_demand_for_tables_{slot}")
#             new_demand = _to_int(new_demand, 0)
#             available_total = getattr(ts, f"number_of_tables_available_{slot}")
#             available_total = _to_int(available_total, 0)
#             left = available_total - new_demand
#             pretty_slot = SLOT_LABELS.get(slot, slot)

#             logger.info(
#                 "Reservation confirmed for %s %s at %s on %s (phone=%s, user_id=%s)",
#                 first_name,
#                 last_name,
#                 pretty_slot,
#                 date,
#                 request.user.is_staff,
#                 user_for_reservation.id if user_for_reservation else None,
#             )

#             # -----------------------------------
#             # 6. Send confirmation email
#             # -----------------------------------
#             if email:
#                 try:
#                     if request.user.is_staff:
#                         template_name = "reservation_book/emails/phone_reservation_confirmation.txt"
#                     else:
#                         template_name = "reservation_book/emails/reservation_confirmation.txt"

#                     message = render_to_string(
#                         template_name,
#                         {
#                             "reservation": reservation,
#                             "time_slot_pretty": pretty_slot,
#                             "tables_needed": tables_needed,
#                             "left": left,
#                         },
#                     )

#                     send_mail(
#                         subject="Your Gambinos reservation is confirmed",
#                         message=message,
#                         from_email=settings.DEFAULT_FROM_EMAIL,
#                         recipient_list=[email],
#                         fail_silently=False,
#                     )
#                     logger.info(
#                         "Confirmation email sent to %s for reservation id=%s",
#                         email,
#                         reservation.id,
#                     )
#                 except Exception as e:
#                     logger.exception(
#                         "Error sending reservation confirmation email: %s", e
#                     )

#             # -----------------------------------
#             # 7. Response (AJAX or normal)
#             # -----------------------------------
#             if is_ajax:
#                 return JsonResponse(
#                     {
#                         "success": True,
#                         "reservation_id": reservation.id,
#                         "date": str(date),
#                         "pretty_slot": pretty_slot,
#                         "demand": new_demand,
#                         "available": available_total,
#                         "left": left,
#                     }
#                 )

#             messages.success(request, "Reservation confirmed!")
#             return redirect("make_reservation")

#         except Exception as e:
#             logger.exception(
#                 "Unexpected error in make_reservation POST: %s", e)
#             if request.headers.get("x-requested-with") == "XMLHttpRequest":
#                 return JsonResponse({"success": False, "error": str(e)})
#             messages.error(request, f"Error processing reservation: {e}")
#             return redirect("make_reservation")

#     # GET branch – same as before
#     next_30_days = _build_next_30_days()

#     return render(
#         request,
#         "reservation_book/make_reservation.html",
#         {
#             "next_30_days": next_30_days,
#             "slot_labels": SLOT_LABELS,
#         },
#     )

# reservation_book/views.py


def _slot_key_set():
    """
    SLOT_LABELS is typically a list/tuple of (key, label) pairs.
    We accept only keys from SLOT_LABELS to prevent invalid start_slot values.
    """
    try:
        return {k for (k, _label) in SLOT_LABELS}
    except Exception:
        # If SLOT_LABELS is a dict, fallback:
        try:
            return set(SLOT_LABELS.keys())
        except Exception:
            return set()


def _choose_online_email_template():
    """
    Prefer an online/customer template if it exists in templates/.
    Falls back to your existing reservation_confirmation.txt.
    """
    candidates = [
        # If you added these, keep them here:
        "reservation_book/online_reservation_confirmation.txt",
        "reservation_book/reservation_confirmation_customer.txt",
        # Fallback (your existing one):
        "reservation_book/reservation_confirmation.txt",
    ]

    for name in candidates:
        try:
            # render_to_string will raise TemplateDoesNotExist if not present
            render_to_string(name, {})
            return name
        except Exception:
            continue

    # Absolute last resort:
    return "reservation_book/reservation_confirmation.txt"


# @login_required
# def make_reservation(request):
#     """
#     Customer-facing online reservation flow.
#     Fixes Heroku 500 by NOT using Customer(user=...).
#     Creates TableReservation rows (and optional ReservationSeries),
#     then emails the customer a confirmation with a "My Reservations" link.
#     """
#     user = request.user

#     if not user.email:
#         messages.error(
#             request, "Your account must have an email address to book online.")
#         # or wherever you manage email in allauth
#         return redirect("account_email")

#     valid_slot_keys = _slot_key_set()

#     # Basic page context (used by template to render the grid / dropdowns)
#     # NOTE: Your template may expect different variable names; keep slot_labels at minimum.
#     context = {
#         "slot_labels": SLOT_LABELS,
#     }

#     if request.method == "GET":
#         return render(request, "reservation_book/make_reservation.html", context)

#     # -------------------------
#     # POST: create reservation(s)
#     # -------------------------
#     reservation_date_str = request.POST.get(
#         "reservation_date") or request.POST.get("date")
#     start_slot = request.POST.get(
#         "start_slot") or request.POST.get("slot") or ""

#     duration_slots_raw = request.POST.get(
#         "duration_slots") or request.POST.get("duration_hours") or "1"
#     series_days_raw = request.POST.get("series_days") or "1"
#     tables_raw = request.POST.get(
#         "tables") or request.POST.get("num_tables") or "1"

#     phone = (request.POST.get("phone") or "").strip()
#     first_name = (request.POST.get("first_name")
#                   or user.first_name or "").strip()
#     last_name = (request.POST.get("last_name") or user.last_name or "").strip()
#     notes = (request.POST.get("notes") or "").strip()

#     # Validate date
#     try:
#         reservation_date = timezone.datetime.fromisoformat(
#             reservation_date_str).date()
#     except Exception:
#         messages.error(request, "Please choose a valid reservation date.")
#         return render(request, "reservation_book/make_reservation.html", context)

#     # Validate slot key
#     if valid_slot_keys and start_slot not in valid_slot_keys:
#         messages.error(request, "Please choose a valid time slot.")
#         return render(request, "reservation_book/make_reservation.html", context)

#     # Parse ints safely
#     try:
#         duration_slots = max(1, int(duration_slots_raw))
#     except Exception:
#         duration_slots = 1

#     try:
#         series_days = max(1, int(series_days_raw))
#     except Exception:
#         series_days = 1

#     try:
#         num_tables = max(1, int(tables_raw))
#     except Exception:
#         num_tables = 1

#     with transaction.atomic():
#         # IMPORTANT: Customer has no 'user' FK in your models, so use email.
#         customer, _created = Customer.objects.get_or_create(
#             email=user.email,
#             defaults={
#                 "first_name": first_name,
#                 "last_name": last_name,
#                 "phone": phone,
#                 "mobile": "",
#                 "notes": "",
#             },
#         )

#         # Keep customer details fresh (optional but useful)
#         changed = False
#         if first_name and customer.first_name != first_name:
#             customer.first_name = first_name
#             changed = True
#         if last_name and customer.last_name != last_name:
#             customer.last_name = last_name
#             changed = True
#         if phone and customer.phone != phone:
#             customer.phone = phone
#             changed = True
#         if notes and (not customer.notes or notes not in (customer.notes or "")):
#             # Append notes rather than overwrite
#             customer.notes = (customer.notes + "\n"
#                               + notes).strip() if customer.notes else notes
#             changed = True
#         if changed:
#             customer.save()

#         # Create a series if booking spans multiple days OR duration > 1 (optional but helpful)
#         series = None
#         if series_days > 1:
#             series = ReservationSeries.objects.create(
#                 customer=customer,
#                 created_by=user,
#                 title="Online reservation series",
#                 notes=notes,
#             )

#         reservations_created = []
#         for day_offset in range(series_days):
#             day = reservation_date + timedelta(days=day_offset)
#             r = TableReservation.objects.create(
#                 customer=customer,
#                 reservation_date=day,
#                 start_slot=start_slot,
#                 duration_slots=duration_slots,
#                 tables=num_tables,
#                 source="online",
#                 booked_by_staff=False,
#                 reservation_series=series,
#                 notes=notes,
#                 created_by=user,
#             )
#             reservations_created.append(r)

#     # Build manage link for email + template context
#     manage_url = request.build_absolute_uri(reverse("my_reservations"))

#     # Email: use online/customer template if you added it; fallback otherwise
#     template_name = _choose_online_email_template()

#     primary = reservations_created[0]
#     email_ctx = {
#         "customer": customer,
#         "reservation": primary,
#         "reservations": reservations_created,  # if your new template wants to list all
#         "manage_url": manage_url,
#         "site_name": "Gambinos Restaurant & Lounge",
#         "is_online": True,
#     }

#     subject = "Your table reservation at Gambinos Restaurant & Lounge"
#     body = render_to_string(template_name, email_ctx)

#     send_mail(
#         subject=subject,
#         message=body,
#         from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
#         recipient_list=[user.email],
#         fail_silently=False,
#     )

#     messages.success(
#         request, "Reservation created! Please check your email for confirmation.")
#     return redirect("my_reservations")
@login_required
def make_reservation(request):
    """
    Customer-facing online reservation view.

    IMPORTANT:
    - Do NOT query Customer.user (not all customers are users).
    - Canonical lookup for the logged-in user is by email (and optionally phone).
    - Send a confirmation email listing ALL selected slots + link to My Reservations.
    """

    user = request.user
    user_email = (getattr(user, "email", "") or "").strip().lower()

    if not user_email:
        messages.error(
            request, "Your account is missing an email address. Please update it and try again.")
        # change if you have a profile route
        return redirect("account_profile")

    # --- Pull availability grid (keep your existing approach if you already have helpers) ---
    # If your project already has these helpers, keep using them.
    # Otherwise, you can remove this block and just render the template with whatever you use now.
    try:
        from .utils import build_week_grid, get_next_7_days  # adjust if different
        week_days = get_next_7_days()
        availability_grid = build_week_grid(week_days)
    except Exception:
        week_days = []
        availability_grid = []

    if request.method == "GET":
        return render(
            request,
            "reservation_book/make_reservation.html",  # adjust template path if different
            {
                "availability_grid": availability_grid,
                "week_days": week_days,
                "slot_labels": SLOT_LABELS,  # handy for template rendering
            },
        )

    # -------------------------
    # POST: create reservation(s)
    # -------------------------
    # e.g. ["2026-02-13|17_18", "2026-02-14|17_18"]
    selected_slots = request.POST.getlist("selected_slots")
    tables_requested = request.POST.get(
        "tables_requested") or request.POST.get("num_tables") or "1"

    try:
        tables_requested = int(tables_requested)
    except ValueError:
        tables_requested = 1

    if tables_requested < 1:
        tables_requested = 1

    if not selected_slots:
        messages.error(request, "Please select at least one time slot.")
        return redirect("make_reservation")

    # Canonical customer lookup: by email (NOT user FK)
    # Keep user->customer link implicit (via email), since not all customers are users.
    customer, _created = Customer.objects.get_or_create(
        email=user_email,
        defaults={
            "first_name": (getattr(user, "first_name", "") or "").strip(),
            "last_name": (getattr(user, "last_name", "") or "").strip(),
        },
    )

    # Optional: keep names fresh if blank
    dirty = False
    if not customer.first_name and getattr(user, "first_name", ""):
        customer.first_name = user.first_name.strip()
        dirty = True
    if not customer.last_name and getattr(user, "last_name", ""):
        customer.last_name = user.last_name.strip()
        dirty = True
    if dirty:
        customer.save(update_fields=["first_name", "last_name"])

    # Create a series for grouping (optional but recommended if you already have it)
    series = ReservationSeries.objects.create(
        customer=customer,
        created_by=user,  # if your model has this; otherwise remove
        source="online",  # if your model has this; otherwise remove
        notes="Online reservation",
    )

    created_reservations = []
    slot_lines_for_email = []  # list of (date_str, label_str, reservation_id)

    # Selected slot string format expected: "YYYY-MM-DD|SLOT_KEY"
    for item in selected_slots:
        try:
            date_str, slot_key = item.split("|", 1)
        except ValueError:
            continue

        # Convert date
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        # Get label from SLOT_LABELS (don’t use SLOT_KEYS)
        slot_label = SLOT_LABELS.get(slot_key, slot_key)

        # If your TableReservation model stores a key (slot_key) and date, do that.
        # Adjust field names below to match your model.
        reservation = TableReservation.objects.create(
            customer=customer,
            reservation_series=series,
            date=day,
            time_slot=slot_key,          # if your field is named differently, adjust
            tables_reserved=tables_requested,
            created_by=user,             # remove if not in your model
            source="online",             # remove if not in your model
        )
        created_reservations.append(reservation)
        slot_lines_for_email.append(
            (day.strftime("%b. %d, %Y"), slot_label, reservation.id))

    if not created_reservations:
        series.delete()
        messages.error(
            request, "No valid time slots were submitted. Please try again.")
        return redirect("make_reservation")

    # -------------------------
    # Confirmation email (online)
    # -------------------------
    my_reservations_url = request.build_absolute_uri(
        reverse("my_reservations"))

    # Group lines by date for nicer email formatting
    grouped = defaultdict(list)
    for d, label, rid in slot_lines_for_email:
        grouped[d].append((label, rid))

    # Build a plain-text block with ALL dates/slots
    lines = []
    for d in grouped:
        lines.append(f"- Date: {d}")
        for label, rid in grouped[d]:
            lines.append(f"  • Time: {label} (Reservation ID: {rid})")
    reservations_block = "\n".join(lines)

    subject = "Your table reservation at Gambinos Restaurant & Lounge"

    # If you want to use ONE template file, keep it generic and pass context.
    # Otherwise create a dedicated template e.g. reservation_confirmation_customer.txt.
    try:
        body_txt = render_to_string(
            # create this template (recommended)
            "emails/reservation_confirmation_customer.txt",
            {
                "customer_name": f"{customer.first_name} {customer.last_name}".strip() or customer.email,
                "tables_requested": tables_requested,
                "reservations_block": reservations_block,
                "my_reservations_url": my_reservations_url,
                "was_made_by_staff": False,
            },
        )
    except Exception:
        # Fallback if template doesn't exist yet
        body_txt = (
            f"Dear {(customer.first_name + ' ' + customer.last_name).strip() or customer.email},\n\n"
            f"Thank you for your reservation at Gambinos Restaurant & Lounge.\n"
            f"This reservation was made online.\n\n"
            f"Here are your reservation details:\n\n"
            f"{reservations_block}\n\n"
            f"Number of tables: {tables_requested}\n\n"
            f"You can view or manage your reservations here:\n"
            f"{my_reservations_url}\n\n"
            f"We look forward to welcoming you!\n\n"
            f"Best regards,\n"
            f"Gambinos Restaurant & Lounge\n"
        )

    msg = EmailMultiAlternatives(
        subject=subject,
        body=body_txt,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        to=[customer.email],
    )
    msg.send(fail_silently=False)

    messages.success(
        request, "Reservation created! A confirmation email has been sent.")
    return redirect("my_reservations")


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
def my_reservations(request):
    """
    Customer-facing list of reservations.

    IMPORTANT:
    TableReservation has no `user` FK in your project, so we match by:
        request.user.email -> Customer.email -> TableReservation.customer
    """
    customer = _customer_for_logged_in_user(request)

    if not customer:
        messages.error(
            request,
            "We couldn't find a customer profile for your account email. "
            "Please contact the restaurant.",
        )
        reservations = TableReservation.objects.none()
    else:
        # ACTIVE only (new status + legacy boolean)
        reservations = (
            TableReservation.objects
            .active()
            .filter(customer=customer)
            .select_related("customer", "timeslot_availability")
            .order_by("reservation_date", "time_slot", "id")
        )

    # Attach any template-friendly display flags without touching model @properties
    for r in reservations:
        # Your template uses reservation.status_display already.
        # It is now provided by the model property, so no need to compute here.
        pass

    return render(
        request,
        "reservation_book/my_reservations.html",
        {
            "customer": customer,
            "reservations": reservations,
            "slot_labels": SLOT_LABELS,
        },
    )
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
def staff_reservations(request):
    """
    Staff-facing list of reservations.

    IMPORTANT:
    - TableReservation has NO `user` FK in your project.
    - Staff can view all ACTIVE reservations.
    - Display must reflect multi-hour blocks using duration_hours.

    Fixes:
    - Do NOT assign to model @property (e.g. time_range_pretty) -> can raise:
        "property ... has no setter"
    - Instead, attach a separate display-only attribute:
        reservation.time_range_display
    """

    # -----------------------------------------
    # Pull ACTIVE reservations only
    # -----------------------------------------
    qs = (
        TableReservation.objects
        .active()
        .select_related("customer", "timeslot_availability")
        .order_by("reservation_date", "time_slot", "id")
    )

    # -----------------------------------------
    # Helper: compute "HH:MM–HH:MM" for duration
    # -----------------------------------------
    def _pretty_time_range(start_slot_key: str, duration_hours: int) -> str:
        """
        Convert (start_slot_key + duration_hours) -> "HH:MM–HH:MM".

        Uses SLOT_KEYS ordering to walk forward across slots.
        Falls back safely to the base slot label if anything is missing.
        """
        start_label = SLOT_LABELS.get(start_slot_key, start_slot_key)

        try:
            dur = int(duration_hours or 1)
        except Exception:
            dur = 1
        if dur <= 1:
            return start_label

        slot_keys = list(SLOT_KEYS) if SLOT_KEYS else list(SLOT_LABELS.keys())

        if start_slot_key not in slot_keys:
            return start_label

        start_index = slot_keys.index(start_slot_key)
        end_index = min(start_index + dur - 1, len(slot_keys) - 1)
        end_slot_key = slot_keys[end_index]
        end_label = SLOT_LABELS.get(end_slot_key, end_slot_key)

        try:
            start_time = start_label.split("–")[0].strip()
            end_time = end_label.split("–")[1].strip()
            return f"{start_time}–{end_time}"
        except Exception:
            return start_label

    # -----------------------------------------
    # Attach display-only attributes for template
    # -----------------------------------------
    reservations = list(qs)

    for r in reservations:
        r.time_range_display = _pretty_time_range(
            r.time_slot,
            getattr(r, "duration_hours", 1),
        )

        # Optional: staff template may want a friendly status label
        # (model now provides r.status_display)
        # r.status_display is safe to use directly.

    return render(
        request,
        "reservation_book/staff_reservations.html",
        {
            "reservations": reservations,
            "slot_labels": SLOT_LABELS,
        },
    )


@superuser_required
def staff_management(request):
    staff_users = User.objects.filter(
        is_staff=True).order_by('last_name', 'first_name')
    return render(request, 'reservation_book/staff_management.html', {
        'staff_users': staff_users,
    })


@superuser_required
def add_staff(request):
    """Add a new staff member or re-activate an existing one"""
    if request.method == 'POST':

        first_name = (request.POST.get('first_name') or "").strip()
        last_name = (request.POST.get('last_name') or "").strip()
        email = _normalize_email(request.POST.get('email'))

        if not all([first_name, last_name, email]):
            messages.error(request, "All fields are required.")
            return redirect('staff_management')

        temp_password = get_random_string(length=12)

        existing_user = User.objects.filter(email=email).first()

        if existing_user:
            if existing_user.is_staff:
                messages.error(request, "This user is already a staff member.")
                return redirect('staff_management')
            else:
                # Re-activate former staff
                existing_user.first_name = first_name
                existing_user.last_name = last_name
                existing_user.is_staff = True
                existing_user.is_active = True
                existing_user.set_password(temp_password)
                existing_user.save()
                messages.success(
                    request, f"Staff member {first_name} {last_name} re-activated and new password emailed.")
        else:
            # Create new staff
            User.objects.create_user(
                username=email,
                email=email,
                first_name=first_name,
                last_name=last_name,
                password=temp_password,
                is_staff=True,
                is_active=True
            )
            messages.success(
                request, f"Staff member {first_name} {last_name} added and password emailed.")

        # Send welcome email
        login_url = request.build_absolute_uri(reverse('account_login'))
        subject = "Gambino's Restaurant - Staff Account Access"
        message = f"""
Hello {first_name},

Your staff account for Gambino's Restaurant & Bar has been created (or re-activated).

Please log in using the details below and change your password immediately:

Login URL: {login_url}
Username: {email}
Temporary Password: {temp_password}

For security, please change your password after logging in.

Thank you,
Management
        """

        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email],
                fail_silently=False,
            )
        except Exception as e:
            messages.warning(
                request, f"Staff added but email failed: {str(e)}")
            logger.error(f"Email failed for {email}: {str(e)}")

        return redirect('staff_management')

    return redirect('staff_management')


@superuser_required
def remove_staff(request, user_id):
    """Completely remove a staff member from the database"""
    user = get_object_or_404(User, id=user_id)

    if user.is_superuser:
        messages.error(request, "Cannot remove a superuser.")
    elif user == request.user:
        messages.error(request, "You cannot remove yourself.")
    else:
        full_name = user.get_full_name() or user.email
        user.delete()
        messages.success(
            request, f"Staff member {full_name} completely removed.")

    return redirect('staff_management')


@login_required
def first_login_setup(request):
    """Force new staff to change username and password on first login"""
    if not request.user.is_staff:
        return redirect('make_reservation')

    # If username has been changed from email, they've already completed setup
    if request.user.username != request.user.email:
        return redirect('staff_dashboard')

    if request.method == 'POST':
        password_form = SetPasswordForm(request.user, request.POST)
        username = request.POST.get('username', '').strip()

        if password_form.is_valid() and username:
            user = password_form.save()
            update_session_auth_hash(request, user)  # Keep logged in

            user.username = username
            user.save()

            messages.success(
                request, "Password and username updated successfully. Welcome!")
            return redirect('staff_dashboard')
    else:
        password_form = SetPasswordForm(request.user)

    return render(request, 'reservation_book/first_login_setup.html', {
        'password_form': password_form,
        'current_email': request.user.email,
    })


@staff_or_superuser_required
def staff_dashboard(request):
    today = timezone.localdate()

    total_reservations = TableReservation.objects.count()
    upcoming_reservations_count = TableReservation.objects.filter(
        reservation_date__gte=today,
        reservation_status=True,
    ).count()
    phone_reservations_count = TableReservation.objects.filter(
        is_phone_reservation=True
    ).count()
    registered_customers_count = Customer.objects.count()
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

    customers = (
        Customer.objects.filter(reservations__isnull=False)
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
        {"customers": customers},
    )


@staff_or_superuser_required
def user_reservation_history(request, customer_id):
    """
    Staff view: full reservation history for a given registered customer.
    """
    history_customer = get_object_or_404(Customer, id=customer_id)
    reservations = (
        TableReservation.objects.filter(customer=history_customer)
        .select_related("timeslot_availability")
        .order_by(
            "-reservation_date",
            "-time_slot",
        )
    )

    return render(
        request,
        "reservation_book/user_reservation_history.html",
        {
            "history_customer": history_customer,
            "reservations": reservations,
        },
    )


@staff_or_superuser_required
def create_phone_reservation(request):
    """Staff UI for creating reservations for phone-in customers (Option B + time blocks).

    Rules enforced:
    - Reuse existing Customer by email
    - Reuse or create auth User
    - NEVER email passwords
    - If Customer is NEW (first time in DB) => ALWAYS send set-password onboarding link
    - Else if User has unusable password => send set-password onboarding link
    - Else => send login link
    - Ensure allauth EmailAddress exists and is primary+verified
    - Supports:
        * single-day time blocks (duration_hours OR 'until close')
        * conference series: same block for N consecutive days
    """
    User = get_user_model()
    next_30_days = _build_next_30_days()

    if request.method == "POST":
        form = PhoneReservationForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Please correct the errors below.")
            print("PHONE FORM ERRORS:", form.errors)
            return render(
                request,
                "reservation_book/create_phone_reservation.html",
                {"form": form, "slot_labels": SLOT_LABELS,
                    "next_30_days": next_30_days},
            )

        # contains an UNSAVED Customer instance
        proto = form.save(commit=False)
        proto.is_phone_reservation = True
        proto.created_by = request.user

        # ----------------------------
        # Normalize + upsert Customer
        # ----------------------------
        raw_customer = proto.customer

        email = _normalize_email(getattr(raw_customer, "email", ""))

        if not email:
            messages.error(request, "Customer email is required.")
            return render(
                request,
                "reservation_book/create_phone_reservation.html",
                {"form": form, "slot_labels": SLOT_LABELS,
                    "next_30_days": next_30_days},
            )

        customer_defaults = {
            "first_name": (getattr(raw_customer, "first_name", "") or "").strip(),
            "last_name": (getattr(raw_customer, "last_name", "") or "").strip(),
            "phone": getattr(raw_customer, "phone", "") or "",
            "mobile": getattr(raw_customer, "mobile", "") or "",
        }

        customer, created_customer = Customer.objects.get_or_create(
            email=email,
            defaults={**customer_defaults, "email": email},
        )

        # Only update fields if we have non-empty values (avoid wiping good data)
        cust_changed = False
        for field, val in customer_defaults.items():
            if val and getattr(customer, field, "") != val:
                setattr(customer, field, val)
                cust_changed = True
        if cust_changed:
            customer.save()

        # ----------------------------
        # Booking parameters (READ FROM cleaned_data, not proto)
        # ----------------------------
        start_date = form.cleaned_data.get("reservation_date")
        start_slot = form.cleaned_data.get("time_slot")
        tables_needed = int(form.cleaned_data.get(
            "number_of_tables_required_by_patron") or 0)

        if not start_date or not start_slot:
            messages.error(
                request,
                "Please click a date/time cell in the availability grid before confirming.",
            )
            return render(
                request,
                "reservation_book/create_phone_reservation.html",
                {"form": form, "slot_labels": SLOT_LABELS,
                    "next_30_days": next_30_days},
            )

        slots = _slot_order()
        if start_slot not in slots:
            messages.error(request, "Invalid time slot selection.")
            return render(
                request,
                "reservation_book/create_phone_reservation.html",
                {"form": form, "slot_labels": SLOT_LABELS,
                    "next_30_days": next_30_days},
            )

        start_index = slots.index(start_slot)

        # Duration: either explicit duration_hours, or 'book_until_close'
        max_choice = max(int(c[0]) for c in TableReservation._meta.get_field(
            "duration_hours").choices)

        until_close = bool(form.cleaned_data.get(
            "book_until_close"))  # ✅ correct name
        if until_close:
            duration = len(slots) - start_index
            duration = max(1, min(duration, max_choice))
        else:
            duration = int(form.cleaned_data.get("duration_hours") or 1)
            duration = max(1, min(duration, max_choice))

        series_days = int(form.cleaned_data.get("series_days") or 1)
        series_days = max(1, min(series_days, 14))

        affected_slots = slots[start_index: start_index + duration]

        # ----------------------------
        # Timeslot + availability + User handling (atomic)
        # ----------------------------
        created_reservations = []
        user = None

        try:
            with transaction.atomic():
                # Find or create user (once per booking)
                user = (
                    User.objects.filter(email__iexact=email).first()
                    or User.objects.filter(username__iexact=email).first()
                )

                if user is None:
                    user = User.objects.create_user(
                        username=email,
                        email=email,
                        password=None,
                        first_name=customer.first_name,
                        last_name=customer.last_name,
                    )
                    user.set_unusable_password()
                    user.save(update_fields=["password"])
                else:
                    # Safe sync of missing profile bits
                    user_changed = False
                    if not user.first_name and customer.first_name:
                        user.first_name = customer.first_name
                        user_changed = True
                    if not user.last_name and customer.last_name:
                        user.last_name = customer.last_name
                        user_changed = True
                    if not user.email:
                        user.email = email
                        user_changed = True
                    if user_changed:
                        user.save()

                # Ensure allauth EmailAddress exists and is usable for auth flows
                ea, _ = EmailAddress.objects.get_or_create(
                    user=user,
                    email=email,
                    defaults={"primary": True, "verified": True},
                )
                needs_ea_update = False
                if not ea.primary:
                    ea.primary = True
                    needs_ea_update = True
                if not ea.verified:
                    ea.verified = True
                    needs_ea_update = True
                if needs_ea_update:
                    ea.save(update_fields=["primary", "verified"])

                # Book N consecutive days
                for day_offset in range(series_days):
                    day = start_date + datetime.timedelta(days=day_offset)

                    ts, _ = TimeSlotAvailability.objects.get_or_create(
                        calendar_date=day,
                        defaults=_timeslot_defaults(),
                    )
                    # Lock row for consistent demand updates
                    ts = TimeSlotAvailability.objects.select_for_update().get(pk=ts.pk)

                    # Capacity check across all affected slots
                    for s in affected_slots:
                        slot_available = _to_int(
                            getattr(ts, f"number_of_tables_available_{s}", 0), 0)
                        slot_demand = _to_int(
                            getattr(ts, f"total_cust_demand_for_tables_{s}", 0), 0)
                        if slot_demand + tables_needed > slot_available:
                            raise ValueError(
                                f"Not enough tables available for {day} in slot {SLOT_LABELS.get(s, s)}."
                            )

                    # Create ONE reservation row per day (start slot + duration_hours)
                    r = TableReservation(
                        customer=customer,
                        timeslot_availability=ts,
                        reservation_date=day,
                        time_slot=start_slot,
                        duration_hours=duration,
                        number_of_tables_required_by_patron=tables_needed,
                        reservation_status=True,
                        is_phone_reservation=True,
                        created_by=request.user,
                    )
                    r.save()
                    created_reservations.append(r)

                    # Update demand for each affected slot
                    update_fields = []
                    for s in affected_slots:
                        demand_field = f"total_cust_demand_for_tables_{s}"
                        current = _to_int(getattr(ts, demand_field, 0), 0)
                        setattr(ts, demand_field, current + tables_needed)
                        update_fields.append(demand_field)
                    ts.save(update_fields=update_fields)

        except ValueError as e:
            messages.error(request, str(e))
            return render(
                request,
                "reservation_book/create_phone_reservation.html",
                {"form": form, "slot_labels": SLOT_LABELS,
                    "next_30_days": next_30_days},
            )

        # ----------------------------
        # Email decision (Option B)
        # ----------------------------
        login_url = request.build_absolute_uri(reverse("account_login"))

        # New CUSTOMER in DB => onboarding link
        # Existing customer but unusable password => onboarding link
        needs_password_setup = bool(created_customer) or (
            not user.has_usable_password())

        password_setup_url = None
        if needs_password_setup:
            password_setup_url = _build_set_password_link(request, user)

        # Pretty time range (start–end)
        def _range_pretty(slots_list):
            if not slots_list:
                return ""
            first_label = SLOT_LABELS.get(slots_list[0], slots_list[0])
            last_label = SLOT_LABELS.get(slots_list[-1], slots_list[-1])
            try:
                start_t = first_label.split("–")[0].strip()
                end_t = last_label.split("–")[1].strip()
                return f"{start_t}–{end_t}"
            except Exception:
                return first_label

        time_range_pretty = _range_pretty(
            affected_slots) if duration > 1 else SLOT_LABELS.get(start_slot, start_slot)

        context = {
            "reservation": created_reservations[0] if created_reservations else proto,
            "reservations": created_reservations,
            "time_slot_pretty": time_range_pretty,
            "tables_needed": tables_needed,
            "login_url": login_url,
            "needs_password_setup": needs_password_setup,
            "password_setup_url": password_setup_url,
            "series_days": series_days,
            "duration_hours": duration,
            "until_close": until_close,
        }

        message = render_to_string(
            "reservation_book/emails/phone_reservation_confirmation.txt",
            context,
        )

        send_mail(
            subject="Your reservation at Gambinos Restaurant & Lounge is confirmed",
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )

        messages.success(request, "Phone reservation created.")
        return redirect("staff_dashboard")

    # GET
    form = PhoneReservationForm()
    return render(
        request,
        "reservation_book/create_phone_reservation.html",
        {"form": form, "slot_labels": SLOT_LABELS, "next_30_days": next_30_days},
    )


def _normalize_query(q: str) -> str:
    """
    - strips
    - collapses whitespace
    - removes whitespace around '@' in emails ("name @gmail.com" → "name@gmail.com")
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
                date_val = res_by_id.reservation_date or (
                    res_by_id.timeslot_availability.calendar_date if res_by_id.timeslot_availability else None
                )
                customer_name = ""
                customer_email = ""
                customer_phone = ""
                customer_mobile = ""
                if res_by_id.customer:
                    customer_name = f"{res_by_id.customer.first_name} {res_by_id.customer.last_name}".strip(
                    )
                    customer_email = res_by_id.customer.email or ""
                    customer_phone = res_by_id.customer.phone or ""
                    customer_mobile = res_by_id.customer.mobile or ""

                reservation_by_id = {
                    "type": "reservation",
                    "reservation_id": res_by_id.id,
                    "first_name": customer_name.split()[0] if customer_name else "",
                    "last_name": " ".join(customer_name.split()[1:]) if customer_name else "",
                    "email": customer_email,
                    "phone": customer_phone,
                    "mobile": customer_mobile,
                    "reservation_date": date_val.isoformat() if date_val else "",
                    "time_slot": res_by_id.time_slot or "",
                    "pretty_slot": SLOT_LABELS.get(res_by_id.time_slot, res_by_id.time_slot or ""),
                    "reservation_status": bool(getattr(res_by_id, "reservation_status", True)),
                }
        except (ValueError, OverflowError):
            pass

    if reservation_by_id:
        results.append(reservation_by_id)

    # ------------------------------------------------------------------
    # Mode: existing → active/upcoming reservations
    # ------------------------------------------------------------------
    if mode == "existing":
        reservations_qs = (
            TableReservation.objects
            .select_related("timeslot_availability", "customer")
            .filter(reservation_status=True, reservation_date__gte=today)
            .filter(
                Q(customer__first_name__icontains=q)
                | Q(customer__last_name__icontains=q)
                | Q(customer__email__icontains=q)
            )
            .order_by("-reservation_date", "-created_at")[:10]
        )

        for r in reservations_qs:
            if reservation_by_id and r.id == reservation_by_id["reservation_id"]:
                continue

            date_val = r.reservation_date or (
                r.timeslot_availability.calendar_date if r.timeslot_availability else None
            )
            customer_name = ""
            if r.customer:
                customer_name = f"{r.customer.first_name} {r.customer.last_name}".strip(
                )

            results.append({
                "type": "reservation",
                "reservation_id": r.id,
                "first_name": customer_name.split()[0] if customer_name else "",
                "last_name": " ".join(customer_name.split()[1:]) if customer_name else "",
                "email": r.customer.email if r.customer else "",
                "phone": r.customer.phone if r.customer else "",
                "mobile": r.customer.mobile if r.customer else "",
                "reservation_date": date_val.isoformat() if date_val else "",
                "time_slot": r.time_slot or "",
                "pretty_slot": SLOT_LABELS.get(r.time_slot, r.time_slot or ""),
                "reservation_status": True,
            })

        return JsonResponse({"results": results})

    # ------------------------------------------------------------------
    # Mode: past (default) → customer profiles from Customer model
    # ------------------------------------------------------------------
    customer_filter = (
        Q(email__iexact=q)
        | Q(email__icontains=q)
        | Q(first_name__icontains=q)
        | Q(last_name__icontains=q)
    )

    customers_qs = Customer.objects.filter(
        customer_filter).order_by("last_name", "first_name")[:15]

    customer_results = []
    seen_emails = set()

    for c in customers_qs:
        email_lower = (c.email or "").lower()
        if email_lower in seen_emails:
            continue
        if email_lower:
            seen_emails.add(email_lower)

        customer_results.append({
            "type": "customer",
            "first_name": c.first_name or "",
            "last_name": c.last_name or "",
            "email": c.email or "",
            "phone": c.phone or "",
            "mobile": c.mobile or "",
        })

    results.extend(customer_results)

    return JsonResponse({"results": results})


def _build_set_password_link(request, user):
    """
    Builds a one-time onboarding link that lets a user SET their password (not reset-request flow).
    """
    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return request.build_absolute_uri(
        reverse("onboarding_set_password", kwargs={
                "uidb64": uidb64, "token": token})
    )


@require_http_methods(["GET", "POST"])
def onboarding_set_password(request, uidb64, token):
    """
    One-time onboarding link: user sets password, then we log them in.
    Works even with multiple AUTHENTICATION_BACKENDS configured.
    """
    User = get_user_model()

    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except Exception:
        user = None

    if user is None or not default_token_generator.check_token(user, token):
        messages.error(
            request, "This password setup link is invalid or has expired.")
        return redirect("account_login")

    if request.method == "POST":
        form = SetPasswordForm(user, request.POST)
        if form.is_valid():
            form.save()

            new_password = form.cleaned_data["new_password1"]

            # Authenticate so Django knows which backend was used (fixes multi-backend login() errors)
            authed = authenticate(
                request, username=user.get_username(), password=new_password)
            if authed is None and getattr(user, "email", None):
                authed = authenticate(
                    request, email=user.email, password=new_password)

            if authed is not None:
                login(request, authed)
            else:
                # Fallback: explicitly specify backend (keeps you from crashing)
                login(
                    request,
                    user,
                    backend="allauth.account.auth_backends.AuthenticationBackend",
                )

            messages.success(
                request, "Password set successfully. You’re now logged in.")
            return redirect("my_reservations")

    else:
        form = SetPasswordForm(user)

    return render(
        request,
        "reservation_book/onboarding_set_password.html",
        {"form": form, "user": user},
    )


@staff_or_superuser_required
@require_http_methods(["POST"])
def resend_password_setup_link(request):
    """
    Staff button: resend the onboarding set-password link to a customer by email.
    Expected POST field: email
    """

    email = _normalize_email(request.POST.get("email"))
    if not email:
        messages.error(request, "Email is required.")
        # change if your staff page name differs
        return redirect("staff_management")

    User = get_user_model()
    user = (
        User.objects.filter(email__iexact=email).first()
        or User.objects.filter(username__iexact=email).first()
    )

    if not user:
        messages.error(request, "No user found for that email.")
        return redirect("staff_management")

    # Ensure EmailAddress row exists for allauth
    EmailAddress.objects.get_or_create(
        user=user,
        email=email,
        defaults={"primary": True, "verified": True},
    )

    password_setup_url = _build_set_password_link(request, user)

    # You can reuse your existing phone confirmation template or create a small staff resend template
    context = {"password_setup_url": password_setup_url, "user": user}
    message = render_to_string(
        "reservation_book/emails/resend_password_setup.txt", context)

    send_mail(
        subject="Set your password for Gambinos Restaurant & Lounge",
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )

    messages.success(request, "Password setup link resent.")
    return redirect("staff_management")
