from __future__ import annotations
from django.db.models import Count, Sum, Q, F, IntegerField, Value
from datetime import date
import json
import uuid
from django.shortcuts import render, redirect
from django.db import transaction
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
from django.utils.http import url_has_allowed_host_and_scheme
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
from django.db.models import Q, Count, Sum, F, ImageField, Value
from django.template.loader import render_to_string
from django.http import JsonResponse
from django.db.models.expressions import ExpressionWrapper
from django.db.models.functions import Coalesce
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify

from django.views.decorators.http import require_GET, require_http_methods
from django.views.decorators.http import require_POST
from django.utils import timezone

from django.utils.crypto import get_random_string
from django.http import HttpResponseForbidden
from allauth.account.models import EmailAddress

from .decorators import staff_or_superuser_required
from .constants import SLOT_LABELS
# from .models import SLOT_KEYS
from .models import TimeSlotAvailability, RestaurantConfig, Customer, ReservationSeries, TableReservation
from .models import CancellationEvent, ReservationStats, NoShowEvent

from .forms import (
    PhoneReservationForm,
    EditReservationForm,
    ReservationForm,
)


# SLOT_KEYS = list(SLOT_LABELS.keys())


def _default_tables_per_slot() -> int:
    """
    Central place for your capacity-per-slot default.

    Priority:
    1) settings.DEFAULT_TABLES_PER_SLOT (if you define it)
    2) settings.TABLES_PER_SLOT
    3) fallback = 20
    """
    for attr in ("DEFAULT_TABLES_PER_SLOT", "TABLES_PER_SLOT"):
        val = getattr(settings, attr, None)
        if isinstance(val, int) and val > 0:
            return val
        # allow strings like "20" in env-configured settings
        if isinstance(val, str) and val.isdigit() and int(val) > 0:
            return int(val)
    return 20


def _get_slot_capacity_default():
    """
    Your project already has a notion of default tables per slot (often 20).
    Keep this as a single place to change it.

    If you already have _default_tables_per_slot(), you can replace the body with:
        return _default_tables_per_slot()
    """
    return 20


def home(request):
    return render(request, 'reservation_book/index.html')


def _normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def menu(request):
    return render(request, "reservation_book/menu.html")


def _customer_for_logged_in_user(user):
    """Match by email – your Customer model has NO user FK."""
    if not user or not getattr(user, "is_authenticated", False):
        return None

    email = (getattr(user, "email", "") or "").strip().lower()
    if not email:
        return None

    return Customer.objects.filter(email__iexact=email).first()


def staff_or_superuser_required(view_func):
    """
    Custom decorator: allow access if user is staff OR superuser.
    Also requires authenticated and active.
    """
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(settings.LOGIN_URL)
        if not request.user.is_active:
            messages.error(
                request, "Account inactive.")
            return redirect("home")

        if request.user.is_superuser or request.user.is_staff:
            return view_func(request, *args, **kwargs)
        messages.error(
            request, "Staff access required.")
        return redirect("my_reservations")
    return wrapper


def _timeslot_defaults(default_tables_per_slot: int = 20) -> dict:
    """
    Defaults for a NEW TimeSlotAvailability row.

    Your model fields:
      - calendar_date (PK)
      - number_of_tables_available_<slot>
      - total_cust_demand_for_tables_<slot>
    """
    d = {}
    for key in SLOT_LABELS.keys():
        d[f"number_of_tables_available_{key}"] = default_tables_per_slot
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


def _safe_int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


# --- SLOT LABELS --- moved to constants.py


def _slot_order():
    """
    Returns slot keys in display order, based purely on SLOT_LABELS.
    """
    return list(SLOT_LABELS.keys())


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
        slot_keys = list(SLOT_LABELS)  # uses your global ordering if present
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


def _auto_complete_past_reservations():
    """
    Past reservations should not remain 'active'.
    This is idempotent and safe to call on list pages.
    """
    today = timezone.localdate()

    qs = TableReservation.objects.filter(reservation_date__lt=today)

    # Only flip ones that are still effectively active
    if hasattr(TableReservation, "status"):
        qs = qs.filter(status=TableReservation.STATUS_ACTIVE)
        qs.update(status=TableReservation.STATUS_COMPLETED)

    # If your legacy boolean is used, flip it off for past ones (optional, but keeps UI sane)
    if hasattr(TableReservation, "reservation_status"):
        TableReservation.objects.filter(
            reservation_date__lt=today,
            reservation_status=True,
        ).update(reservation_status=False)


@login_required
@staff_or_superuser_required
@require_POST
def mark_reservation_completed(request, reservation_id):
    """
    Staff marks reservation as COMPLETED at bill payment.
    Rule: can ONLY be completed on the reservation date.
    """
    reservation = get_object_or_404(TableReservation, id=reservation_id)

    today = timezone.localdate()
    if reservation.reservation_date != today:
        messages.error(
            request, "Complete can only be set on the reservation date.")
        return redirect("staff_reservations")

    status_active = getattr(TableReservation, "STATUS_ACTIVE", "active")
    status_completed = getattr(
        TableReservation, "STATUS_COMPLETED", "completed")
    status_no_show = getattr(TableReservation, "STATUS_NO_SHOW", "no_show")

    # Only allow completing ACTIVE reservations (not no_show, not already completed)
    if getattr(reservation, "status", None) != status_active:
        if getattr(reservation, "status", None) == status_completed:
            messages.info(request, "This reservation is already completed.")
        elif getattr(reservation, "status", None) == status_no_show:
            messages.error(
                request, "Cannot complete a reservation already marked as No Show.")
        else:
            messages.info(request, "This reservation is not active.")
        return redirect("staff_reservations")

    updates = []
    reservation.status = status_completed
    updates.append("status")

    # Keep legacy boolean TRUE (your legacy meaning is “not cancelled”)
    if hasattr(reservation, "reservation_status"):
        if reservation.reservation_status is not True:
            reservation.reservation_status = True
            updates.append("reservation_status")

    reservation.save(update_fields=updates)

    messages.success(request, "Reservation marked as completed.")
    return redirect("staff_reservations")


@staff_or_superuser_required
@require_POST
def mark_completed(request, reservation_id):
    """
    Alias view so older templates/urls using 'mark_completed' keep working.
    """
    reservation = get_object_or_404(TableReservation, id=reservation_id)

    today = timezone.localdate()

    # ---------- lifecycle guards ----------
    if reservation.status != TableReservation.STATUS_ACTIVE:
        messages.error(request, "Only active reservations can be completed.")
        return redirect("staff_reservations")

    if reservation.reservation_date != today:
        messages.error(request, "Completion allowed only on reservation date.")
        return redirect("staff_reservations")

    return mark_reservation_completed(request, reservation_id)


NO_SHOW_BAN_THRESHOLD = 3  # adjust


def _auto_mark_no_shows(today=None):
    """
    Past reservations that are still ACTIVE become NO_SHOW automatically.
    - Cancelled reservations are hard-deleted, so they never appear here.
    - Completed reservations are excluded.
    """
    today = today or timezone.localdate()

    if not hasattr(TableReservation, "STATUS_ACTIVE"):
        return

    status_active = TableReservation.STATUS_ACTIVE
    status_completed = getattr(
        TableReservation, "STATUS_COMPLETED", "completed")  # unused but fine
    status_no_show = getattr(TableReservation, "STATUS_NO_SHOW", "no_show")

    qs = TableReservation.objects.select_related("customer").filter(
        reservation_date__lt=today,
        status=status_active,
    )

    for r in qs.iterator():
        with transaction.atomic():
            # Lock the reservation row ONLY (avoid outer-join FOR UPDATE issues)
            r2 = (
                TableReservation.objects
                .select_for_update()
                .get(pk=r.pk)
            )

            # re-check under lock
            if r2.reservation_date >= today:
                continue
            if r2.status != status_active:
                continue

            # ✅ G: normalize email consistently for analytics joins
            cust_email = ""
            if r2.customer_id:
                cust_email = (
                    Customer.objects
                    .filter(pk=r2.customer_id)
                    .values_list("email", flat=True)
                    .first()
                    or ""
                )
            cust_email = (cust_email or "").strip().lower()

            event, created = NoShowEvent.objects.get_or_create(
                reservation_id=r2.id,
                defaults={
                    "reservation_date": r2.reservation_date,
                    "time_slot": r2.time_slot or "",
                    "tables": int(getattr(r2, "number_of_tables_required_by_patron", 0) or 0),
                    "duration_slots": int(getattr(r2, "duration_hours", 1) or 1),
                    "customer_email": cust_email,
                    "marked_by_staff": False,
                },
            )

            # ---- created-only side effects ----
            if not created:
                # Event already exists => don't re-mark or re-increment
                continue

            # mark NO_SHOW
            r2.status = status_no_show
            r2.save(update_fields=["status"])

            # increment customer + optional bar
            if r2.customer_id:
                c = Customer.objects.select_for_update().get(pk=r2.customer_id)
                c.no_show_count = int(getattr(c, "no_show_count", 0) or 0) + 1

                update_fields = ["no_show_count"]
                if (not getattr(c, "barred", False)) and c.no_show_count >= NO_SHOW_BAN_THRESHOLD:
                    c.barred = True
                    update_fields.append("barred")

                c.save(update_fields=update_fields)
            # ----------------------------------


def _apply_ban_if_needed(customer_email: str, threshold: int = 3, window_days: int = 90) -> bool:
    """
    Auto-ban customer if they have >= threshold no-shows in the last window_days.
    Returns True if customer was newly barred.
    """
    if not customer_email:
        return False

    cutoff = timezone.now() - timedelta(days=window_days)
    count = NoShowEvent.objects.filter(
        customer_email__iexact=customer_email,
        created_at__gte=cutoff,
    ).count()

    if count >= threshold:
        cust = Customer.objects.filter(email__iexact=customer_email).first()
        if cust and not getattr(cust, "barred", False):
            cust.barred = True
            cust.save(update_fields=["barred"])
            return True

    return False


@staff_or_superuser_required
def bar_customer(request, customer_id):
    if request.method != "POST":
        return redirect("user_reservations_overview")

    c = get_object_or_404(Customer, pk=customer_id)
    if not c.barred:
        c.barred = True
        c.save(update_fields=["barred"])
        messages.success(
            request, f"{c.first_name} {c.last_name} has been barred.")
    else:
        messages.info(request, "Customer is already barred.")

    return redirect("user_reservations_overview")


@staff_or_superuser_required
def unbar_customer(request, customer_id):
    if request.method != "POST":
        return redirect("user_reservations_overview")

    c = get_object_or_404(Customer, pk=customer_id)
    if c.barred:
        c.barred = False
        c.save(update_fields=["barred"])
        messages.success(
            request, f"{c.first_name} {c.last_name} has been unbarred.")
    else:
        messages.info(request, "Customer is not barred.")

    return redirect("user_reservations_overview")


@login_required
def my_reservations(request):

    user = request.user
    logger.warning(f"User email: {user.email}")

    customer = Customer.objects.filter(email__iexact=user.email).first()
    logger.warning(
        f"Found customer: {customer} (ID: {customer.pk if customer else 'None'})"
    )

    today = timezone.localdate()
    _auto_mark_no_shows(today=today)

    # --- Auto-complete past ACTIVE reservations (keeps UI sane) ---
    # Only if your project has a status system. If you only rely on reservation_status,
    # we'll still *display* completed based on date in the template later, but here we
    # do the canonical status transition when possible.
    if not customer:
        messages.info(
            request,
            "No reservations found yet. Make your first booking to see them here.",
        )
        reservations = TableReservation.objects.none()
    else:
        qs = TableReservation.objects.filter(customer=customer)

        # ✅ Status-based filtering ONLY (no legacy reservation_status)
        # Cancelled reservations are hard-deleted (mentor requirement), so they won't appear here anyway.
        if hasattr(TableReservation, "STATUS_ACTIVE"):
            allowed = [TableReservation.STATUS_ACTIVE]

            # include completed/no-show if your model defines them
            if hasattr(TableReservation, "STATUS_COMPLETED"):
                allowed.append(TableReservation.STATUS_COMPLETED)
            if hasattr(TableReservation, "STATUS_NO_SHOW"):
                allowed.append(TableReservation.STATUS_NO_SHOW)

            qs = qs.filter(status__in=allowed)

        reservations = qs.order_by("reservation_date", "time_slot")

        logger.warning(f"Reservations found: {reservations.count()}")
        for r in reservations:
            logger.warning(
                f"Res {r.id}: status={getattr(r, 'status', None)}, legacy={getattr(r, 'reservation_status', None)}"
            )

    context = {
        "reservations": reservations,
        "customer": customer,
        "has_reservations": reservations.exists(),
        "today": today,  # useful for template “Completed” rendering if needed
    }
    return render(request, "reservation_book/my_reservations.html", context)


def cancel_reservation(request, reservation_id):
    """
    Cancel a reservation and release demand back to availability,
    then HARD DELETE the reservation from the database.

    We keep cancellation analytics in a separate model (CancellationEvent),
    so staff dashboard can still show "Cancelled" count even after deletes.

    IMPORTANT:
    TableReservation has NO `user` FK. Permissions via email match:
      request.user.email -> Customer.email -> reservation.customer
    """
    reservation = get_object_or_404(TableReservation, id=reservation_id)

    today = timezone.localdate()
    staff_redirect = "staff_reservations" if request.user.is_staff else "my_reservations"

    # ---------- lifecycle guards ----------
    if reservation.status in (
        TableReservation.STATUS_COMPLETED,
        TableReservation.STATUS_NO_SHOW,
    ):
        messages.error(request, "This reservation cannot be cancelled.")
        return redirect(staff_redirect)

    if reservation.status == TableReservation.STATUS_ACTIVE and reservation.reservation_date < today:
        messages.error(request, "Past reservations cannot be cancelled.")
        return redirect(staff_redirect)

    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    if not _reservation_edit_allowed(request, reservation):
        msg = "You cannot cancel this reservation."
        if is_ajax:
            return JsonResponse({"success": False, "error": msg}, status=403)
        messages.error(request, msg)
        return redirect(staff_redirect)

    # Snapshot everything needed BEFORE we mutate/delete anything
    cancelled_by_staff = bool(request.user.is_staff)

    recipient_email = _reservation_contact_email(reservation)
    guest_name = _reservation_contact_name(
        reservation, fallback_user=request.user)

    res_id = reservation.id
    res_date = reservation.reservation_date
    res_slot = reservation.time_slot
    tables = int(
        getattr(reservation, "number_of_tables_required_by_patron", 0) or 0)
    duration_slots = int(getattr(reservation, "duration_hours", 1) or 1)

    # ✅ G: normalize email for consistent analytics joins
    customer_email = (
        getattr(getattr(reservation, "customer", None), "email", "") or ""
    ).strip().lower()

    customer_id = reservation.customer_id

    # -------- Idempotency --------
    already_cancelled = (
        getattr(reservation, "reservation_status", True) is False)

    try:
        with transaction.atomic():
            # Release demand only if this reservation was still "active"
            if not already_cancelled:
                _cancel_and_release(reservation)

            # --- F2: idempotent cancellation analytics ---
            event, created = CancellationEvent.objects.get_or_create(
                reservation_id=res_id,
                defaults={
                    "created_at": timezone.now(),
                    "reservation_date": res_date,
                    "time_slot": res_slot or "",
                    "tables": tables,
                    "duration_slots": duration_slots,
                    "customer_email": customer_email,
                    "cancelled_by_staff": cancelled_by_staff,
                },
            )

            # Only increment customer counter if we actually created a NEW event
            if created and customer_id:
                Customer.objects.filter(pk=customer_id).update(
                    cancellations_count=F("cancellations_count") + 1
                )
            # --------------------------------------------

            # HARD DELETE (mentor requirement)
            reservation.delete()

    except Exception:
        logger.exception("Cancel/delete failed")
        msg = "Cancellation failed. Please try again."
        if is_ajax:
            return JsonResponse({"success": False, "error": msg}, status=500)
        messages.error(request, msg)
        return redirect(staff_redirect)

    # -------- Optional: send cancellation email --------
    try:
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

            when_str = fmt_day_slot(res_date, res_slot)

            lines = []
            if guest_name:
                lines.append(f"Hello {guest_name},")
                lines.append("")

            lines.append(
                f"Your reservation for {tables} table{plural_s(tables)} on {when_str} has been cancelled."
            )
            lines.append("")
            lines.append(f"Reservation ID: {res_id}")
            if cancelled_by_staff:
                lines.append(f"Cancelled by: STAFF ({request.user.username})")
            else:
                lines.append(f"Cancelled by: {request.user.username}")
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
    except Exception:
        logger.exception("Cancellation email failed")

    if is_ajax:
        return JsonResponse({"success": True})

    messages.success(request, "Reservation cancelled.")
    return redirect(staff_redirect)


@staff_or_superuser_required
def mark_no_show(request, reservation_id):
    """
    Staff marks a reservation as NO SHOW.
    - Does NOT release demand (it's in the past / the slot time passed)
    - Logs a NoShowEvent (idempotent)
    - Auto-bans customer if repeated no-shows
    """
    if request.method != "POST":
        return redirect("staff_reservations")

    reservation = get_object_or_404(TableReservation, id=reservation_id)

    today = timezone.localdate()
    if reservation.reservation_date >= today:
        messages.error(
            request, "No-show can only be set for past reservations.")
        return redirect("staff_reservations")

    if reservation.status in (
        TableReservation.STATUS_COMPLETED,
        TableReservation.STATUS_NO_SHOW,
    ):
        messages.error(request, "Reservation cannot be marked as No Show.")
        return redirect("staff_reservations")

    # If already no_show, do nothing
    if getattr(reservation, "status", "") == TableReservation.STATUS_NO_SHOW:
        messages.info(
            request, "This reservation is already marked as No Show.")
        return redirect("staff_reservations")

    # ✅ G: normalize email for consistent analytics joins
    cust_email = (
        getattr(getattr(reservation, "customer", None), "email", "") or ""
    ).strip().lower()

    # Mark reservation as no-show
    updates = ["status"]
    reservation.status = TableReservation.STATUS_NO_SHOW

    # Keep legacy flag TRUE so old filters don't hide the record
    if hasattr(reservation, "reservation_status"):
        reservation.reservation_status = True
        updates.append("reservation_status")

    reservation.save(update_fields=updates)

    # --- F2: idempotent event creation (exact field match) ---
    event, created = NoShowEvent.objects.get_or_create(
        reservation_id=reservation.id,
        defaults={
            "reservation_date": reservation.reservation_date,
            "time_slot": reservation.time_slot or "",
            "tables": int(getattr(reservation, "number_of_tables_required_by_patron", 0) or 0),
            "duration_slots": int(getattr(reservation, "duration_hours", 1) or 1),
            "customer_email": cust_email,
            "marked_by_staff": True,  # manual staff action
        },
    )
    # --------------------------------------------------------

    # Only apply ban logic if a NEW no-show event was created
    if created:
        newly_barred = _apply_ban_if_needed(
            cust_email, threshold=3, window_days=90)
        if newly_barred:
            messages.warning(
                request, "Customer has been barred due to repeated no-shows.")
    else:
        messages.info(
            request, "No-show event already existed (no duplicate recorded).")

    messages.success(request, "Marked as No Show.")
    return redirect("staff_reservations")


@login_required
def update_reservation(request, reservation_id):
    """
    Customer edit reservation.

    IMPORTANT:
    TableReservation has NO `user` FK. Permissions are enforced via:
      request.user.email -> Customer.email -> reservation.customer

    Notes:
    - Duration and tables are now editable.
    - Date and time slot are NOT editable (avoids availability conflicts).
    - Demand is correctly updated for multi-hour bookings.
    """
    def _safe_next_url(request, default_name="my_reservations"):
        nxt = request.POST.get("next") or request.GET.get("next")
        if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
            return nxt
        return reverse(default_name)

    # Decide default "return to" based on role
    default_return = "staff_reservations" if request.user.is_staff else "my_reservations"

    reservation = get_object_or_404(TableReservation, id=reservation_id)
    today = timezone.localdate()

    if reservation.status in (
        TableReservation.STATUS_COMPLETED,
        TableReservation.STATUS_NO_SHOW,
    ):
        messages.error(request, "This reservation can no longer be edited.")
        return redirect(_safe_next_url(
            request,
            default_name=(
                "staff_reservations" if request.user.is_staff else "my_reservations"),
        ))

    if reservation.status == TableReservation.STATUS_ACTIVE and reservation.reservation_date < today:
        messages.error(request, "Past reservations cannot be edited.")
        return redirect(_safe_next_url(
            request,
            default_name=(
                "staff_reservations" if request.user.is_staff else "my_reservations"),
        ))

    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    # ---------- Permissions ----------
    if not _reservation_edit_allowed(request, reservation):
        msg = "You are not allowed to edit this reservation."
        if is_ajax:
            return JsonResponse({"success": False, "error": msg}, status=403)
        messages.error(request, msg)
        return redirect(_safe_next_url(request, default_name=default_return))

    # ---------- Edit ----------
    if request.method == "POST":
        form = EditReservationForm(request.POST, instance=reservation)

        if not form.is_valid():
            if is_ajax:
                return JsonResponse({"success": False, "error": "Please correct the errors."}, status=400)
            messages.error(request, "Please correct the errors below.")
            return render(
                request,
                "reservation_book/edit_reservation.html",
                {
                    "form": form,
                    "reservation": reservation,
                    "slot_labels": SLOT_LABELS,
                    "next": _safe_next_url(request, default_name=default_return),
                },
            )

        # Save the form (duration_hours + tables)
        reservation = form.save(commit=False)

        # NEW values from form
        new_duration = form.cleaned_data.get(
            "duration_hours", reservation.duration_hours)
        new_tables = form.cleaned_data.get(
            "number_of_tables_required_by_patron",
            reservation.number_of_tables_required_by_patron,
        )

        # Always keep existing date and slot (they are not editable in this form)
        new_date = reservation.reservation_date
        new_slot = reservation.time_slot

        try:
            _apply_reservation_change(
                reservation,
                new_date=new_date,
                new_start_slot=new_slot,
                new_duration=new_duration,
                new_tables_needed=new_tables,
            )
        except ValueError as e:
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
                    "next": _safe_next_url(request, default_name=default_return),
                },
            )

        # Final save
        reservation.save()

        # Optional: send update email (you can keep your existing code here)

        if is_ajax:
            return JsonResponse({"success": True})

        messages.success(request, "Your reservation has been updated.")
        return redirect(_safe_next_url(request, default_name=default_return))

    # GET
    form = EditReservationForm(instance=reservation)
    current_slot_label = SLOT_LABELS.get(
        reservation.time_slot, reservation.time_slot)

    # Debug (remove after testing)
    print("Form fields:", list(form.fields.keys()))
    print("Rendering edit page for reservation", reservation.id)

    return render(
        request,
        "reservation_book/edit_reservation.html",
        {
            "form": form,
            "reservation": reservation,
            "current_slot_label": current_slot_label,
            "slot_labels": SLOT_LABELS,
            "next": _safe_next_url(request, default_name=default_return),
        },
    )


@transaction.atomic
def _apply_reservation_change(
    reservation,
    *,
    new_date,
    new_start_slot,
    new_duration,
    new_tables_needed,
):
    old_ts = TimeSlotAvailability.objects.select_for_update().get(
        pk=reservation.timeslot_availability_id
    )

    # resolve / create new day record
    new_ts, _ = TimeSlotAvailability.objects.get_or_create(
        calendar_date=new_date,
        defaults=_timeslot_defaults(),
    )
    new_ts = TimeSlotAvailability.objects.select_for_update().get(pk=new_ts.pk)

    old_slots = _affected_slots(
        reservation.time_slot, reservation.duration_hours or 1, until_close=False
    )
    old_tables = _to_int(reservation.number_of_tables_required_by_patron, 0)

    new_slots = _affected_slots(
        new_start_slot, new_duration or 1, until_close=False
    )
    new_tables = _to_int(new_tables_needed, 0)

    # ✅ Determine "active" from STATUS, not legacy boolean
    status_active = getattr(TableReservation, "STATUS_ACTIVE", "active")
    is_active = (getattr(reservation, "status", None) == status_active)

    # 1) release old demand (only if active)
    if is_active:
        _update_ts_demand(old_ts, old_slots, old_tables, delta_sign=-1)

    # 2) capacity check on new TS
    ok, bad_slot, avail, demand = _capacity_ok(new_ts, new_slots, new_tables)
    if not ok:
        # put old demand back (since we released it above)
        if is_active:
            _update_ts_demand(old_ts, old_slots, old_tables, delta_sign=+1)
        raise ValueError(
            f"Not enough tables for {new_date} slot {SLOT_LABELS.get(bad_slot, bad_slot)}."
        )

    # 3) save new reservation values
    reservation.reservation_date = new_date
    reservation.timeslot_availability = new_ts
    reservation.time_slot = new_start_slot
    reservation.duration_hours = new_duration
    reservation.number_of_tables_required_by_patron = new_tables
    reservation.save()

    # 4) consume new demand (only if active)
    if is_active:
        _update_ts_demand(new_ts, new_slots, new_tables, delta_sign=+1)


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


logger = logging.getLogger(__name__)

# IMPORTANT: SLOT_LABELS comes from .constants
# SLOT_LABELS = {"17_18": "17:00–18:00", ...}


def _build_next_30_days(days=30):
    today = timezone.localdate()
    defaults = _timeslot_defaults()          # your existing helper
    out = []

    for i in range(days):
        d = today + timezone.timedelta(days=i)
        ts = TimeSlotAvailability.objects.filter(calendar_date=d).first()

        slots = []
        for key, label in SLOT_LABELS.items():
            capacity = int(defaults.get(key, 0))
            if ts is None:
                demand = 0
            else:
                capacity = int(
                    getattr(ts, f"number_of_tables_available_{key}", capacity) or capacity)
                demand = int(
                    getattr(ts, f"total_cust_demand_for_tables_{key}", 0) or 0)

            remaining = max(capacity - demand, 0)

            slots.append({
                "key": key,
                "label": label,
                "available": capacity,      # total capacity (stays 20)
                "remaining": remaining,     # what the template expects
            })

        out.append({
            "calendar_date": d,
            "slots": slots,
            "pk": ts.pk if ts else None,
        })

    return out


logger = logging.getLogger(__name__)


def get_or_create_customer_for_request(request, form):
    """
    Ensures we have a Customer record for stats/forecasting.

    Rules:
    - If user is authenticated: try to map user -> Customer (via your existing helper if present).
    - Otherwise (or if no mapping): use form fields (email/phone/name) to find/create a Customer.
    """
    # 1) If logged in, try to map to an existing Customer
    user = getattr(request, "user", None)
    if user and getattr(user, "is_authenticated", False):
        # If you already have this helper in the file, use it
        if "_customer_for_logged_in_user" in globals():
            customer = _customer_for_logged_in_user(user)
            if customer:
                return customer

        # Fallback: try to match by email if your User has one
        user_email = (getattr(user, "email", "") or "").strip()
        if user_email:
            customer = Customer.objects.filter(
                email__iexact=user_email).first()
            if customer:
                return customer

    # 2) Not logged in (or no mapping) -> use form data
    cd = getattr(form, "cleaned_data", {}) or {}

    first_name = (cd.get("first_name") or cd.get("fname") or "").strip()
    last_name = (cd.get("last_name") or cd.get("lname") or "").strip()

    # Common field names people use in reservation forms
    email = (cd.get("email") or cd.get("customer_email") or "").strip()
    phone = (cd.get("phone") or cd.get("mobile")
             or cd.get("customer_phone") or "").strip()

    # Prefer lookup by email; otherwise by phone; otherwise create a very basic record.
    if email:
        customer, _ = Customer.objects.get_or_create(
            email__iexact=email,
            defaults={
                "first_name": first_name,
                "last_name": last_name,
                "phone": phone,
            },
        )
        return customer

    if phone:
        customer = Customer.objects.filter(phone=phone).first()
        if customer:
            return customer
        return Customer.objects.create(
            first_name=first_name,
            last_name=last_name,
            phone=phone,
        )

    # Absolute fallback (should be rare)
    return Customer.objects.create(
        first_name=first_name or "Guest",
        last_name=last_name or "",
        phone="",
        email="",
    )


# assumes these already exist in your file
# from .forms import PhoneReservationForm
# from .models import Customer, TimeSlotAvailability
# from .constants import SLOT_LABELS
# logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

# Assumes these exist in your project:
# from .constants import SLOT_LABELS
# from .forms import PhoneReservationForm
# from .models import TimeSlotAvailability, TableReservation, Customer
# from .views_helpers import _timeslot_defaults   (or wherever it lives)


logger = logging.getLogger(__name__)


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


# make sure these imports exist in your file already
# from .forms import PhoneReservationForm
# from .models import Customer, TableReservation, TimeSlotAvailability
# plus: SLOT_LABELS, _build_next_30_days, _timeslot_defaults


@login_required
def make_reservation(request):
    """
    Customer-facing /reserve/ view.

    Key rules:
    - duration_hours is treated as "NUMBER OF TIME SLOTS" (not literal hours)
    - A reservation starting at the last slot has 1 slot max (or 0 if you ever add a “closed” slot)
    - Capacity check + demand deduction happen across ALL affected slots
    - Multi-day series supported via series_days
    """
    next_30_days = _build_next_30_days(days=30)

    # Build initial for GET (and as a fallback for POST if user fields were left blank)
    initial = {}
    if request.user.is_authenticated and not request.user.is_staff:
        initial = {
            "first_name": request.user.first_name or "",
            "last_name": request.user.last_name or "",
            "email": request.user.email or "",
        }

    if request.method == "POST":
        logger.warning("[MR] POST keys=%s", list(request.POST.keys()))

        reservation_date_str = request.POST.get("reservation_date")
        time_slot_key = request.POST.get("time_slot")

        if not reservation_date_str or not time_slot_key:
            messages.error(request, "Missing date or time slot.")
            form = PhoneReservationForm(request.POST)
            return render(
                request,
                "reservation_book/make_reservation.html",
                {"form": form, "next_30_days": next_30_days},
            )

        try:
            reservation_date = timezone.datetime.fromisoformat(
                reservation_date_str).date()
        except Exception:
            messages.error(request, "Invalid date.")
            form = PhoneReservationForm(request.POST)
            return render(
                request,
                "reservation_book/make_reservation.html",
                {"form": form, "next_30_days": next_30_days},
            )

        if time_slot_key not in SLOT_LABELS:
            messages.error(request, "Invalid time slot.")
            form = PhoneReservationForm(request.POST)
            return render(
                request,
                "reservation_book/make_reservation.html",
                {"form": form, "next_30_days": next_30_days},
            )

        # IMPORTANT: bind the POST data (not initial)
        post_data = request.POST.copy()

        # If customer is logged in (not staff), backfill required fields if blanks came through
        # (this prevents “required” errors if your modal fields were readonly or not filled)
        if initial:
            for k, v in initial.items():
                if not post_data.get(k):
                    post_data[k] = v

        form = PhoneReservationForm(post_data)
        if not form.is_valid():
            logger.warning("[MR] form errors=%s", form.errors)
            messages.error(request, "Please correct the errors below.")
            return render(
                request,
                "reservation_book/make_reservation.html",
                {"form": form, "next_30_days": next_30_days},
            )

        cleaned = form.cleaned_data

        tables_requested = int(cleaned.get(
            "number_of_tables_required_by_patron") or 1)

        # duration_hours is treated as "slots"
        requested_duration_slots = int(cleaned.get("duration_hours") or 1)

        series_days = int(cleaned.get("series_days") or 1)
        if series_days < 1:
            series_days = 1
        if series_days > 14:
            series_days = 14  # matches your form max

        # checkbox name compatibility: template might be book_until_close, form might be until_close
        until_close = bool(
            cleaned.get("book_until_close")
            or cleaned.get("until_close")
            or request.POST.get("book_until_close")
            or request.POST.get("until_close")
        )

        # Slot math
        slot_keys = list(SLOT_LABELS.keys())
        start_index = slot_keys.index(time_slot_key)

        # slots remaining INCLUDING the start slot
        max_slots_left_today = max(1, len(slot_keys) - start_index)

        # also respect model field choices (prevents "5 is not one of the available choices")
        duration_field = TableReservation._meta.get_field("duration_hours")
        choice_values = [int(v) for (v, _lbl) in (
            duration_field.choices or []) if str(v).isdigit()]
        max_choice_allowed = max(
            choice_values) if choice_values else max_slots_left_today

        if until_close:
            requested_duration_slots = max_slots_left_today

        duration_slots = max(
            1, min(requested_duration_slots, max_slots_left_today, max_choice_allowed))

        end_index = min(start_index + duration_slots, len(slot_keys))
        affected_slot_keys = slot_keys[start_index:end_index]

        email = (cleaned.get("email") or "").strip().lower()

        try:
            with transaction.atomic():
                # Upsert customer
                customer, _ = Customer.objects.get_or_create(
                    email=email,
                    defaults={
                        "first_name": cleaned.get("first_name", ""),
                        "last_name": cleaned.get("last_name", ""),
                        "phone": cleaned.get("phone", ""),
                        "mobile": cleaned.get("mobile", ""),
                    },
                )
                if getattr(customer, "barred", False):
                    raise ValueError(
                        "You can’t make new reservations from this account. "
                        "Please contact the restaurant if you believe this is a mistake.")

                changed_fields = []
                for f, v in [
                    ("first_name", cleaned.get("first_name")),
                    ("last_name", cleaned.get("last_name")),
                    ("phone", cleaned.get("phone")),
                    ("mobile", cleaned.get("mobile")),
                ]:
                    if v is not None and v != "" and getattr(customer, f) != v:
                        setattr(customer, f, v)
                        changed_fields.append(f)
                if changed_fields:
                    customer.save(update_fields=changed_fields)

                reservations_created = []

                for day_offset in range(series_days):
                    day_date = reservation_date + timedelta(days=day_offset)

                    ts_day, _ = TimeSlotAvailability.objects.get_or_create(
                        calendar_date=day_date,
                        defaults=_timeslot_defaults(),
                    )
                    ts_day = TimeSlotAvailability.objects.select_for_update().get(pk=ts_day.pk)

                    # Capacity check across ALL affected slots
                    for k in affected_slot_keys:
                        cap_field = f"number_of_tables_available_{k}"
                        demand_field = f"total_cust_demand_for_tables_{k}"
                        capacity = int(getattr(ts_day, cap_field, 20) or 20)
                        demand = int(getattr(ts_day, demand_field, 0) or 0)
                        remaining = max(capacity - demand, 0)

                        if remaining < tables_requested:
                            messages.error(
                                request,
                                f"Not enough tables on {day_date.strftime('%b %d, %Y')} "
                                f"for {SLOT_LABELS.get(k, k)}. Only {remaining} left."
                            )
                            return render(
                                request,
                                "reservation_book/make_reservation.html",
                                {"form": form, "next_30_days": next_30_days},
                            )

                    # Deduct demand across duration slots
                    update_fields = []
                    for k in affected_slot_keys:
                        dfield = f"total_cust_demand_for_tables_{k}"
                        existing = int(getattr(ts_day, dfield, 0) or 0)
                        setattr(ts_day, dfield, existing + tables_requested)
                        update_fields.append(dfield)
                    ts_day.save(update_fields=update_fields)

                    # Build kwargs safely (your model has legacy fields in some branches)
                    create_kwargs = dict(
                        customer=customer,
                        reservation_date=day_date,
                        time_slot=time_slot_key,
                        duration_hours=duration_slots,
                        number_of_tables_required_by_patron=tables_requested,
                        timeslot_availability=ts_day,
                    )

                    # status field varies across your history; handle safely
                    if hasattr(TableReservation, "STATUS_ACTIVE"):
                        create_kwargs["status"] = TableReservation.STATUS_ACTIVE
                    else:
                        # many older versions used lower-case 'active'
                        create_kwargs["status"] = "active"

                    # legacy flags used by /my_reservations/ in your logs earlier
                    if hasattr(TableReservation, "reservation_status"):
                        create_kwargs["reservation_status"] = True
                    if hasattr(TableReservation, "is_phone_reservation"):
                        create_kwargs["is_phone_reservation"] = False
                    if hasattr(TableReservation, "created_by"):
                        # created_by is usually staff; keep None for customers
                        create_kwargs["created_by"] = request.user if request.user.is_staff else None

                    reservation = TableReservation.objects.create(
                        **create_kwargs)
                    reservations_created.append(reservation)

        except ValueError as e:
            # Business rule errors (barred, capacity, invalid slot, etc.)
            messages.error(request, str(e))
            return render(
                request,
                "reservation_book/make_reservation.html",
                {"form": form, "next_30_days": next_30_days},
            )

        except Exception:
            logger.exception("[MR] reservation create failed")
            messages.error(
                request, "Looks like you've been barred for excessive no shows or other reasons. Please contact us if you think this is a mistake.")
            return render(
                request,
                "reservation_book/make_reservation.html",
                {"form": form, "next_30_days": next_30_days},
            )

        # Email confirmation
        try:
            to_email = customer.email
            if to_email:
                context = {
                    "customer": customer,
                    "reservations": reservations_created,
                    "reservation": reservations_created[0] if reservations_created else None,
                    "slot_labels": SLOT_LABELS,
                }
                message = render_to_string(
                    "reservation_book/emails/online_reservation_confirmation.txt",
                    context,
                )
                send_mail(
                    subject="Your Gambinos reservation is confirmed",
                    message=message,
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                    recipient_list=[to_email],
                    fail_silently=False,
                )
        except Exception:
            logger.exception("Email failed")

        messages.success(
            request,
            f"Reservation{' series' if series_days > 1 else ''} created successfully!",
        )
        return redirect("my_reservations")

    # GET
    form = PhoneReservationForm(initial=initial)
    return render(
        request,
        "reservation_book/make_reservation.html",
        {"form": form, "next_30_days": next_30_days},
    )
# -------------------------------------------------------------------
# Staff dashboard (cards for Phone Reservations, Customer Stats, etc.)
# -------------------------------------------------------------------


@staff_or_superuser_required
def staff_reservations(request):
    """
    Staff-facing list of reservations.
    - Shows ACTIVE + COMPLETED + NO_SHOW
    - Past ACTIVE reservations remain ACTIVE until staff marks outcome
      (Completed or No Show).
    """
    today = timezone.localdate()
    _auto_mark_no_shows(today=today)

    qs = (
        TableReservation.objects
        .select_related("customer", "timeslot_availability")
        .exclude(reservation_date__isnull=True)
        .exclude(time_slot__isnull=True)
        .exclude(time_slot="")
    )

    # Status system
    if hasattr(TableReservation, "STATUS_ACTIVE"):
        allowed = [TableReservation.STATUS_ACTIVE]
        if hasattr(TableReservation, "STATUS_COMPLETED"):
            allowed.append(TableReservation.STATUS_COMPLETED)
        if hasattr(TableReservation, "STATUS_NO_SHOW"):
            allowed.append(TableReservation.STATUS_NO_SHOW)
        qs = qs.filter(status__in=allowed)

    reservations = list(qs.order_by("-reservation_date", "time_slot", "id"))

    # UI-only helper flags (no DB writes)
    for r in reservations:
        r.time_range_display = r.time_range_pretty
        r.is_past = bool(r.reservation_date and r.reservation_date < today)

    return render(
        request,
        "reservation_book/staff_reservations.html",
        {
            "reservations": reservations,
            "slot_labels": SLOT_LABELS,
            "today": today,
        },
    )


def _pretty_time_range(start_slot: str, duration: int) -> str:
    """
    If duration > 1, show something like '17:00–19:00' based on SLOT_LABELS.
    """
    slots = _slot_order()
    if start_slot not in slots:
        return SLOT_LABELS.get(start_slot, start_slot)

    start_index = slots.index(start_slot)
    affected = slots[start_index: start_index + max(1, duration)]

    if not affected:
        return SLOT_LABELS.get(start_slot, start_slot)

    first_label = SLOT_LABELS.get(affected[0], affected[0])
    last_label = SLOT_LABELS.get(affected[-1], affected[-1])

    # Expect labels like "17:00–18:00"
    try:
        start_t = first_label.split("–")[0].strip()
        end_t = last_label.split("–")[1].strip()
        return f"{start_t}–{end_t}"
    except Exception:
        return first_label


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
    # _auto_complete_past_reservations()
    today = timezone.localdate()
    _auto_mark_no_shows(today=today)

    total_reservations = TableReservation.objects.count()
    # upcoming_reservations_count = TableReservation.objects.filter(
    #     reservation_date__gte=today,
    #     reservation_status=True,
    # ).count()
    upcoming_reservations_count = TableReservation.objects.filter(
        reservation_date__gte=today,
        status=TableReservation.STATUS_ACTIVE,
    ).count()
    phone_reservations_count = TableReservation.objects.filter(
        is_phone_reservation=True
    ).count()
    registered_customers_count = Customer.objects.count()

    # ✅ Mentor requirement: cancellations are deleted, so count comes from stats table
    stats = ReservationStats.get_solo()
    cancelled_reservations_count = CancellationEvent.objects.count()
    no_show_count = NoShowEvent.objects.count()

    context = {
        "stats": stats,
        "total_reservations": total_reservations,
        "upcoming_reservations_count": upcoming_reservations_count,
        "phone_reservations_count": phone_reservations_count,
        "registered_customers_count": registered_customers_count,
        "cancelled_reservations_count": cancelled_reservations_count,
        "no_show_count": no_show_count,

    }

    return render(
        request,
        "reservation_book/staff_dashboard.html",
        context,
    )


@staff_or_superuser_required
def user_reservations_overview(request):
    """
    Staff view: overview list of customers and their reservation/counter stats.

    IMPORTANT:
    - Cancelled reservations are hard-deleted, so "cancelled" must come from Customer.cancellations_count
      (or CancellationEvent), NOT from TableReservation rows.
    - No-shows are tracked on Customer.no_show_count and also optionally in NoShowEvent.
    """
    today = timezone.localdate()

    # NOTE: Do NOT filter(reservations__isnull=False) or the table can appear empty
    customers = (
        Customer.objects.all()
        .annotate(
            # TableReservation rows that still exist (active/completed/no_show)
            total_reservations_db=Count("reservations", distinct=True),

            # Upcoming active operational reservations
            active_reservations=Count(
                "reservations",
                filter=Q(
                    reservations__reservation_date__gte=today,
                    reservations__status=TableReservation.STATUS_ACTIVE,
                ),
                distinct=True,
            ),

            # Tables booked across EXISTING reservations only
            total_tables_booked=Coalesce(
                Sum("reservations__number_of_tables_required_by_patron"),
                0,
            ),

            # Upcoming active tables booked
            active_tables_booked=Coalesce(
                Sum(
                    "reservations__number_of_tables_required_by_patron",
                    filter=Q(
                        reservations__reservation_date__gte=today,
                        reservations__status=TableReservation.STATUS_ACTIVE,
                    ),
                ),
                0,
            ),

            # Bring in counters (these represent deleted cancellations + no-shows)
            cancelled_reservations=Coalesce(
                F("cancellations_count"), Value(0)),
            no_show_reservations=Coalesce(F("no_show_count"), Value(0)),

            # Total “history” count = existing rows + deleted cancellations
            total_reservations=ExpressionWrapper(
                Coalesce(Count("reservations", distinct=True), Value(0))
                + Coalesce(F("cancellations_count"), Value(0)),
                output_field=IntegerField(),
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
    Shows:
      - Remaining TableReservation rows (active/completed/no_show)
      - CancellationEvent rows (because cancellations are deleted)
      - NoShowEvent rows (analytics)
    """
    history_customer = get_object_or_404(Customer, id=customer_id)

    reservations = (
        TableReservation.objects.filter(customer=history_customer)
        .select_related("timeslot_availability")
        .order_by("-reservation_date", "-time_slot")
    )

    cust_email = (history_customer.email or "").strip().lower()

    cancelled_events = CancellationEvent.objects.filter(
        customer_email__iexact=cust_email
    ).order_by("-created_at")

    no_show_events = NoShowEvent.objects.filter(
        customer_email__iexact=cust_email
    ).order_by("-created_at")

    return render(
        request,
        "reservation_book/user_reservation_history.html",
        {
            "history_customer": history_customer,
            "reservations": reservations,
            "cancelled_events": cancelled_events,
            "no_show_events": no_show_events,
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

        # --- E1: barred enforcement (staff phone booking) ---
        if getattr(customer, "barred", False):
            messages.error(
                request, "This customer is barred. New bookings are not allowed.")
            return redirect("staff_dashboard")
        # ---------------------------------------------------

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
            .filter(status=TableReservation.STATUS_ACTIVE, reservation_date__gte=today)
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
                "reservation_status": (r.status == TableReservation.STATUS_ACTIVE),
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
