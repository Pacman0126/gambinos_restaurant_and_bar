from django.shortcuts import render, redirect
from django.contrib.auth import get_user_model, login
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from datetime import timedelta, datetime
import logging
import re
from functools import wraps
from django.db.models import Q
from django.conf import settings
from django.contrib import messages
# from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import login, get_user_model
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
# from django.contrib.auth.models import User

from django.contrib.auth.views import redirect_to_login
from django.contrib.auth.decorators import login_required, user_passes_test


from django.core.mail import send_mail
from django.db.models import Count, Sum
from django.template.loader import render_to_string
from django.http import JsonResponse, HttpResponseForbidden
from django.db.models.functions import Coalesce
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify
from django.urls import reverse
from django.views.decorators.http import require_GET
from django.utils.crypto import get_random_string


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


def _customer_for_logged_in_user(request):
    """
    Returns the Customer record that matches the logged-in user's email.
    If user has no email or no matching Customer exists, returns None.
    """
    if not request.user.is_authenticated:
        return None

    email = (getattr(request.user, "email", "") or "").strip().lower()
    if not email:
        return None

    return Customer.objects.filter(email__iexact=email).first()


def _get_customer_for_request_user(request):
    """
    Map logged-in User -> Customer by email.
    Returns Customer or None.
    """
    email = (getattr(request.user, "email", "") or "").strip().lower()
    if not email:
        return None
    return Customer.objects.filter(email__iexact=email).first()


def _require_customer_profile(request):
    """
    Returns (customer, response).
    If response is not None, return it immediately from the view.
    """
    customer = _get_customer_for_request_user(request)
    if customer:
        return customer, None

    # If they’re logged in but there is no matching Customer record:
    # This can happen if the user signed up online before any Customer record exists,
    # or if emails don’t match.
    messages.error(
        request,
        "We couldn’t find your customer profile yet. Please make a reservation online first "
        "or contact the restaurant so we can link your account."
    )
    return None, redirect("make_reservation")


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
    """
    Cancel flow with your current schema:
    - Staff/superusers can cancel any reservation.
    - A logged-in customer can cancel reservations if the reservation.customer.email
      matches request.user.email (case-insensitive).
    """
    reservation = get_object_or_404(TableReservation, id=reservation_id)
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    # --- Permissions ---
    if request.user.is_staff or request.user.is_superuser:
        allowed = True
    else:
        user_customer = _customer_for_logged_in_user(request)
        allowed = bool(
            user_customer
            and reservation.customer_id
            and reservation.customer.email
            and user_customer.email
            and reservation.customer.email.lower() == user_customer.email.lower()
        )

    if not allowed:
        msg = "You are not allowed to cancel this reservation."
        if is_ajax:
            return JsonResponse({"success": False, "error": msg}, status=403)
        messages.error(request, msg)
        return redirect("my_reservations")

    # Already cancelled?
    if reservation.reservation_status is False:
        if is_ajax:
            return JsonResponse({"success": True})
        messages.info(request, "This reservation has already been cancelled.")
        return redirect("my_reservations")

    # Snapshot OLD values for demand rollback
    old_date = reservation.reservation_date
    old_slot = reservation.time_slot
    old_tables = reservation.number_of_tables_required_by_patron

    # Mark as cancelled
    reservation.reservation_status = False
    reservation.save(update_fields=["reservation_status", "updated_at"])

    # --- Update demand: release tables from that date/slot ---
    ts = TimeSlotAvailability.objects.filter(calendar_date=old_date).first()
    if ts:
        demand_field = f"total_cust_demand_for_tables_{old_slot}"
        current_demand = getattr(ts, demand_field, 0) or 0
        new_demand = max(0, current_demand - (old_tables or 0))
        setattr(ts, demand_field, new_demand)
        ts.save(update_fields=[demand_field])

    # --- Email cancellation notice (optional, safe) ---
    recipient_email = None
    if reservation.customer and reservation.customer.email:
        recipient_email = reservation.customer.email

    if recipient_email:
        try:
            pretty_slot = SLOT_LABELS.get(old_slot, old_slot)

            subject = "Your Gambinos reservation has been cancelled"

            def fmt_dt(dt):
                return dt.strftime("%b %d, %Y at %H:%M:%S")

            created_on = fmt_dt(reservation.created_at)
            cancelled_on = fmt_dt(reservation.updated_at)
            when_str = f"{old_date} at {pretty_slot}"

            guest_name = ""
            if reservation.customer:
                guest_name = (
                    f"{reservation.customer.first_name or ''} {reservation.customer.last_name or ''}".strip()
                    or reservation.customer.email
                )

            lines = []
            if guest_name:
                lines.append(f"Hello {guest_name},")
                lines.append("")

            lines.append(
                f"The reservation (created on {created_on})\n"
                f"for {old_tables} table{'s' if old_tables != 1 else ''} on {when_str}\n"
                f"was cancelled on {cancelled_on}."
            )
            lines.append("")
            lines.append(f"Reservation ID: {reservation.id}")
            if request.user.is_staff or request.user.is_superuser:
                lines.append(f"Cancelled by: STAFF ({request.user.username})")
            else:
                lines.append(f"Cancelled by: {request.user.username}")
            lines.append("")
            lines.append(
                "Thank you for choosing Gambinos Restaurant & Lounge.")

            send_mail(
                subject,
                "\n".join(lines),
                settings.DEFAULT_FROM_EMAIL,
                [recipient_email],
                fail_silently=True,
            )
        except Exception:
            logger.exception("Error sending cancellation email")

    if is_ajax:
        return JsonResponse({"success": True})

    messages.success(request, "Your reservation has been cancelled.")
    return redirect("my_reservations")


@login_required
def my_reservations(request):
    """
    Customer portal: show active reservations for the logged-in user.

    IMPORTANT:
    TableReservation has no `user` FK in your model, so we match by:
        request.user.email -> Customer.email -> TableReservation.customer
    """
    customer = _customer_for_logged_in_user(request)

    if not customer:
        # Customer may have logged in but never made/received a reservation yet,
        # or their email doesn't match any Customer record.
        messages.info(
            request,
            "We couldn't find reservations for your account email yet. "
            "If you made a booking by phone, make sure you log in with the same email address.",
        )
        reservations = TableReservation.objects.none()
    else:
        reservations = (
            TableReservation.objects.filter(
                customer=customer, reservation_status=True)
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
# def staff_or_superuser_required(view_func):
#     """
#     Custom decorator: allow access if user is staff OR superuser.
#     Also requires authenticated and active.
#     """
#     def wrapper(request, *args, **kwargs):
#         if not request.user.is_authenticated:
#             return redirect(settings.LOGIN_URL)
#         if not request.user.is_active:
#             return HttpResponseForbidden("Account inactive.")
#         if request.user.is_superuser or request.user.is_staff:
#             return view_func(request, *args, **kwargs)
#         return HttpResponseForbidden("Staff access required.")
#     return wrapper


def staff_or_superuser_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = request.user

        if not user.is_authenticated:
            return redirect_to_login(request.get_full_path())

        if not user.is_active:
            return HttpResponseForbidden("Account inactive.")

        if user.is_staff or user.is_superuser:
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


def _build_set_password_link(request, user):
    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return request.build_absolute_uri(
        reverse(
            "onboarding_set_password",
            kwargs={"uidb64": uidb64, "token": token},
        )
    )


def onboarding_set_password(request, uidb64, token):
    """
    One-time onboarding link: user sets password, then we log them in.

    Fix:
    - With multiple AUTHENTICATION_BACKENDS, login() must be given a backend.
    - Since you use django-allauth, explicitly use allauth backend here.
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

            # IMPORTANT: Explicit backend fixes "multiple authentication backends" crash
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
def create_phone_reservation(request):
    """
    Staff UI for creating reservations for phone-in customers (Option B).

    Rules enforced:
    - Reuse existing Customer by email
    - Reuse or create auth User
    - NEVER email passwords
    - If Customer is NEW (first time in DB) => ALWAYS send set-password onboarding link
    - Else if User has unusable password => send set-password onboarding link
    - Else => send login link
    - Ensure allauth EmailAddress exists
    """
    User = get_user_model()
    next_30_days = _build_next_30_days()

    if request.method == "POST":
        form = PhoneReservationForm(request.POST)
        if form.is_valid():
            reservation = form.save(commit=False)
            reservation.is_phone_reservation = True
            reservation.created_by = request.user

            # ----------------------------
            # Normalize + upsert Customer
            # ----------------------------
            raw_customer = reservation.customer
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

            reservation.customer = customer

            # ----------------------------
            # Timeslot + User handling
            # ----------------------------
            with transaction.atomic():
                ts, _ = TimeSlotAvailability.objects.get_or_create(
                    calendar_date=reservation.reservation_date,
                    defaults=_timeslot_defaults(),
                )
                reservation.timeslot_availability = ts

                # Find existing user by email or by username=email
                user = (
                    User.objects.filter(email__iexact=email).first()
                    or User.objects.filter(username__iexact=email).first()
                )

                user_created = False

                if user is None:
                    # Create user with UNUSABLE password (must set it via onboarding link)
                    user = User.objects.create_user(
                        username=email,
                        email=email,
                        password=None,
                        first_name=customer.first_name,
                        last_name=customer.last_name,
                    )
                    user.set_unusable_password()
                    user.save(update_fields=["password"])
                    user_created = True
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

                # Ensure allauth EmailAddress exists
                EmailAddress.objects.get_or_create(
                    user=user,
                    email=email,
                    defaults={"primary": True, "verified": True},
                )

                # NOTE: Your TableReservation model does NOT have a `user` FK right now,
                # so we DO NOT try to set reservation.user here.

                reservation.save()

                # Update demand
                slot = reservation.time_slot
                demand_field = f"total_cust_demand_for_tables_{slot}"
                current = getattr(ts, demand_field, 0) or 0
                setattr(
                    ts,
                    demand_field,
                    current
                    + (reservation.number_of_tables_required_by_patron or 0),
                )
                ts.save()

            # ----------------------------
            # Email decision (this is the core fix)
            # ----------------------------
            pretty_slot = SLOT_LABELS.get(
                reservation.time_slot, reservation.time_slot)
            login_url = request.build_absolute_uri(reverse("account_login"))

            # ✅ Option B decision:
            # New CUSTOMER in DB => onboarding link (always)
            # Existing customer but unusable password => onboarding link
            # Else => login link
            needs_password_setup = bool(created_customer) or (
                not user.has_usable_password())

            password_setup_url = None
            if needs_password_setup:
                password_setup_url = _build_set_password_link(request, user)

            context = {
                "reservation": reservation,
                "time_slot_pretty": pretty_slot,
                "tables_needed": reservation.number_of_tables_required_by_patron,
                "login_url": login_url,
                "needs_password_setup": needs_password_setup,
                "password_setup_url": password_setup_url,
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

        messages.error(request, "Please correct the errors below.")
    else:
        form = PhoneReservationForm()

    return render(
        request,
        "reservation_book/create_phone_reservation.html",
        {"form": form, "slot_labels": SLOT_LABELS, "next_30_days": next_30_days},
    )


@login_required
def update_reservation(request, reservation_id):
    """
    Customer edit reservation.

    IMPORTANT:
    TableReservation has NO `user` FK. Permissions are enforced via:
      request.user.email -> Customer.email -> reservation.customer

    Rules:
    - Staff/superuser can edit any reservation.
    - Customer can edit only if reservation.customer.email matches their login email.
    """

    reservation = get_object_or_404(TableReservation, id=reservation_id)
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    # ---------- Permissions ----------
    if request.user.is_staff or request.user.is_superuser:
        allowed = True
    else:
        user_email = (getattr(request.user, "email", "") or "").strip().lower()
        res_email = (
            (reservation.customer.email or "").strip().lower()
            if reservation.customer and reservation.customer.email
            else ""
        )
        allowed = bool(user_email and res_email and user_email == res_email)

    if not allowed:
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
                return JsonResponse({"success": False, "error": "Please correct the errors."}, status=400)
            messages.error(request, "Please correct the errors below.")
            return render(
                request,
                "reservation_book/edit_reservation.html",
                {"form": form, "reservation": reservation,
                    "slot_labels": SLOT_LABELS},
            )

        # Snapshot OLD values for demand rollback
        old_date = reservation.reservation_date
        old_slot = reservation.time_slot
        old_tables = reservation.number_of_tables_required_by_patron or 0

        # Apply changes (not saved yet)
        updated_res = form.save(commit=False)
        new_date = updated_res.reservation_date
        new_slot = updated_res.time_slot
        new_tables = updated_res.number_of_tables_required_by_patron or 0

        try:
            with transaction.atomic():
                # Ensure a timeslot availability row exists for the new date
                new_ts, _ = TimeSlotAvailability.objects.get_or_create(
                    calendar_date=new_date,
                    defaults=_timeslot_defaults(),
                )

                # Check capacity on the NEW slot/date, considering that we may be moving
                new_available = _to_int(
                    getattr(new_ts, f"number_of_tables_available_{new_slot}", 0), 0)
                new_demand = _to_int(
                    getattr(new_ts, f"total_cust_demand_for_tables_{new_slot}", 0), 0)

                # If same date+slot, you can "reuse" your existing tables in the math
                if old_date == new_date and old_slot == new_slot:
                    effective_demand = max(0, new_demand - old_tables)
                else:
                    effective_demand = new_demand

                if effective_demand + new_tables > new_available:
                    msg = "Not enough tables available for that time slot."
                    if is_ajax:
                        return JsonResponse({"success": False, "error": msg}, status=400)
                    messages.error(request, msg)
                    return redirect("my_reservations")

                # Roll back OLD demand
                old_ts = TimeSlotAvailability.objects.filter(
                    calendar_date=old_date).first()
                if old_ts:
                    old_field = f"total_cust_demand_for_tables_{old_slot}"
                    old_current = _to_int(getattr(old_ts, old_field, 0), 0)
                    setattr(old_ts, old_field, max(
                        0, old_current - old_tables))
                    old_ts.save(update_fields=[old_field])

                # Apply NEW demand
                new_field = f"total_cust_demand_for_tables_{new_slot}"
                # Reload demand to avoid using stale pre-rollback numbers if old_ts == new_ts
                new_ts.refresh_from_db()
                new_current = _to_int(getattr(new_ts, new_field, 0), 0)
                setattr(new_ts, new_field, new_current + new_tables)
                new_ts.save(update_fields=[new_field])

                # Save reservation linking
                updated_res.timeslot_availability = new_ts
                # keep denormalized date consistent
                updated_res.reservation_date = new_ts.calendar_date
                updated_res.save()

        except Exception as e:
            if is_ajax:
                return JsonResponse({"success": False, "error": str(e)}, status=500)
            messages.error(request, f"Error updating reservation: {e}")
            return redirect("my_reservations")

        if is_ajax:
            return JsonResponse({"success": True, "reservation_id": updated_res.id})

        messages.success(request, "Reservation updated.")
        return redirect("my_reservations")

    # GET: show form
    form = EditReservationForm(instance=reservation)
    return render(
        request,
        "reservation_book/edit_reservation.html",
        {"form": form, "reservation": reservation, "slot_labels": SLOT_LABELS},
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


@staff_or_superuser_required
@require_GET
def ajax_lookup_customer(request):
    # if not request.user.is_staff:
    #     return JsonResponse({"results": []}, status=403)

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


def _send_set_password_link_email(request, user, recipient_email: str):
    """
    Email a one-time set-password link to the customer.
    Works for brand-new users and users that exist but have no usable password.
    """
    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)

    link = request.build_absolute_uri(
        reverse("onboarding_set_password", args=[uidb64, token])
    )

    subject = "Set your Gambinos password"
    message = (
        f"Hello {user.get_full_name() or user.username},\n\n"
        "An account was created/updated for you so you can manage your reservations.\n\n"
        f"Set your password here (one-time link):\n{link}\n\n"
        "If you did not request this, you can ignore this email.\n\n"
        "— Gambinos Restaurant & Lounge"
    )

    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[recipient_email],
        fail_silently=False,
    )
