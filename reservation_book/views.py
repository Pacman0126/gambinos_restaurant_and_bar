from datetime import timedelta, datetime
import logging
import re

import datetime
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.contrib.auth.tokens import default_token_generator
from django.db.models import Q
from django.conf import settings
from django.contrib import messages
# from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import login, get_user_model, authenticate
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import SetPasswordForm
# from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required, user_passes_test


from django.core.mail import send_mail
from django.db.models import Q, Count, Sum
from django.template.loader import render_to_string
from django.http import JsonResponse
from django.db.models.functions import Coalesce
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods
from django.utils.crypto import get_random_string
from django.http import HttpResponseForbidden
from allauth.account.models import EmailAddress

# from .decorators import staff_or_superuser_required

from .models import TableReservation, TimeSlotAvailability, RestaurantConfig, Customer
from .forms import (
    SignUpForm,
    PhoneReservationForm,
    EditReservationForm,
)
from .constants import SLOT_LABELS
SLOT_KEYS = list(SLOT_LABELS.keys())


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


# def onboarding_set_password(request, uidb64, token):
#     """
#     One-time onboarding link:
#     - user sets a password
#     - then we log them in safely (even with multiple AUTHENTICATION_BACKENDS)

#     URL kwargs must be: uidb64, token
#     """
#     User = get_user_model()

#     try:
#         uid = force_str(urlsafe_base64_decode(uidb64))
#         user = User.objects.get(pk=uid)
#     except Exception:
#         user = None

#     if user is None or not default_token_generator.check_token(user, token):
#         messages.error(
#             request, "This password setup link is invalid or has expired.")
#         return redirect("account_login")

#     if request.method == "POST":
#         form = SetPasswordForm(user, request.POST)
#         if form.is_valid():
#             form.save()

#             # Attach backend properly in multi-backend setups:
#             new_password = form.cleaned_data["new_password1"]

#             # Try authenticating using username
#             authed = authenticate(
#                 request, username=user.get_username(), password=new_password)

#             # Optional: if your auth backend supports email login, try that too
#             if authed is None and getattr(user, "email", None):
#                 authed = authenticate(
#                     request, email=user.email, password=new_password)

#             if authed is not None:
#                 login(request, authed)
#             else:
#                 # Absolute fallback: force a known backend
#                 # If you use allauth backend, swap to:
#                 # "allauth.account.auth_backends.AuthenticationBackend"
#                 login(request, user,
#                       backend="django.contrib.auth.backends.ModelBackend")

#             messages.success(
#                 request, "Password set successfully. You’re now logged in.")
#             return redirect("my_reservations")
#     else:
#         form = SetPasswordForm(user)

#     return render(
#         request,
#         "reservation_book/onboarding_set_password.html",
#         {"form": form, "user": user},
#     )


# def _build_set_password_link(request, user):
#     """Build a one-time onboarding link for setting a password (no temp passwords)."""
#     uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
#     token = default_token_generator.make_token(user)
#     path = reverse("onboarding_set_password", args=[uidb64, token])
#     return request.build_absolute_uri(path)


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

        days.append({
            "date": date,
            "timeslot": ts,
            "slots": slots,
        })

    return days


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
            # 3. Get or create Customer
            # -----------------------------------
            customer_data = {
                'first_name': first_name,
                'last_name': last_name,
                'email': email,
                'phone': phone,
                'mobile': mobile,
            }

            customer, created = Customer.objects.get_or_create(
                email=email,
                defaults=customer_data
            )
            if not created:
                # Update existing customer
                for key, value in customer_data.items():
                    setattr(customer, key, value)
                customer.save()

            # -----------------------------------
            # 4. Decide which user this reservation belongs to (for authenticated users)
            # -----------------------------------
            user_for_reservation = None
            if request.user.is_authenticated and not request.user.is_staff:
                user_for_reservation = request.user

                # Optionally sync user details to customer
                updated = False
                if first_name and not user_for_reservation.first_name:
                    user_for_reservation.first_name = first_name
                    updated = True
                if last_name and not user_for_reservation.last_name:
                    user_for_reservation.last_name = last_name
                    updated = True
                if email.lower() != user_for_reservation.email.lower():
                    user_for_reservation.email = email
                    updated = True
                if updated:
                    user_for_reservation.save()

            # -----------------------------------
            # 5. Create reservation
            # -----------------------------------
            reservation = TableReservation.objects.create(
                is_phone_reservation=request.user.is_staff,
                time_slot=slot,
                number_of_tables_required_by_patron=tables_needed,
                timeslot_availability=ts,
                reservation_status=True,
                reservation_date=ts.calendar_date,
                customer=customer,  # Link to the Customer object we created/updated
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
            # 6. Send confirmation email
            # -----------------------------------
            if email:
                try:
                    if request.user.is_staff:
                        template_name = "reservation_book/emails/phone_reservation_confirmation.txt"
                    else:
                        template_name = "reservation_book/emails/reservation_confirmation.txt"

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
            # 7. Response (AJAX or normal)
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

    # GET branch – same as before
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
        first_name = request.POST.get('first_name').strip()
        last_name = request.POST.get('last_name').strip()
        email = request.POST.get('email').strip().lower()

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
def staff_reservations(request):
    """
    Staff view: search and manage ALL reservations (online + phone-in).

    Search by:
    - Booking ID (exact, if q is all digits)
    - username
    - email (user or customer email)
    - first/last name
    - phone or mobile
    """
    query = request.GET.get("q", "").strip()

    qs = (
        TableReservation.objects.select_related(
            "customer", "timeslot_availability"
        ).order_by(
            "-reservation_date",
            "-time_slot",
            "-created_at",
        )
    )

    if query:
        combined = Q()

        # If it's purely digits, treat as possible Booking ID
        if query.isdigit():
            combined |= Q(id=int(query))

        # Customer fields
        combined |= Q(customer__first_name__icontains=query)
        combined |= Q(customer__last_name__icontains=query)
        combined |= Q(customer__email__icontains=query)
        combined |= Q(customer__phone__icontains=query)
        combined |= Q(customer__mobile__icontains=query)

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
        email = ((getattr(raw_customer, "email", "") or "").strip().lower())
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
                    return datetime.strptime(val, "%Y-m-d").date()
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
            date_str = reservation.reservation_date.strftime("%Y-m-d")
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


# @staff_or_superuser_required
# def resend_password_setup_link(request, customer_id):
#     """
#     Staff action: resend a customer's set-password onboarding link.

#     Sends ONLY if:
#     - The customer exists and has an email
#     - A matching user exists or can be found by email/username=email
#     - The user does NOT have a usable password yet
#     """

#     customer = get_object_or_404(Customer, pk=customer_id)
#     email = (customer.email or "").strip().lower()

#     if not email:
#         messages.error(request, "This customer has no email address on file.")
#         # adjust to your actual staff customers page name
#         return redirect("staff_customers")

#     User = get_user_model()
#     user = (
#         User.objects.filter(email__iexact=email).first()
#         or User.objects.filter(username__iexact=email).first()
#     )

#     if user is None:
#         messages.error(
#             request, "No user account exists for this customer yet.")
#         return redirect("staff_customers")

#     # Ensure allauth EmailAddress exists (helps with login/email flows)
#     ea, _ = EmailAddress.objects.get_or_create(
#         user=user,
#         email=email,
#         defaults={"primary": True, "verified": True},
#     )
#     if not ea.primary or not ea.verified:
#         ea.primary = True
#         ea.verified = True
#         ea.save(update_fields=["primary", "verified"])

#     if user.has_usable_password():
#         messages.info(
#             request, "This customer already has a password set. No link was sent.")
#         return redirect("staff_customers")

#     # Build direct onboarding link
#     password_setup_url = _build_set_password_link(request, user)

#     # Email content (simple + consistent with your other emails)
#     context = {
#         "customer": customer,
#         "password_setup_url": password_setup_url,
#         "login_url": request.build_absolute_uri(reverse("account_login")),
#     }

#     message = render_to_string(
#         "reservation_book/emails/resend_password_setup_link.txt",
#         context,
#     )

#     send_mail(
#         subject="Set up your Gambinos account password",
#         message=message,
#         from_email=settings.DEFAULT_FROM_EMAIL,
#         recipient_list=[email],
#         fail_silently=False,
#     )

#     messages.success(request, f"Password setup link sent to {email}.")
#     return redirect("staff_customers")


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
    email = (request.POST.get("email") or "").strip().lower()
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
