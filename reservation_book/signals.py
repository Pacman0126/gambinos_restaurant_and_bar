import logging
from django.dispatch import receiver
from django.contrib.auth.signals import user_logged_in
from django.utils import timezone
from django.core.cache import cache
from allauth.account.signals import user_signed_up
from .models import TableReservation, Customer
from reservation_book.services.sweeps import run_no_show_sweep

logger = logging.getLogger(__name__)


@receiver(user_signed_up)
def attach_existing_reservations(request, user, **kwargs):
    """
    Robust variant:
    - Normalize email
    - Ensure canonical Customer exists
    - Attach any reservations belonging to any Customer row with this email
      (covers legacy duplicates), but only where created_by is NULL.
    """
    email = ((getattr(user, "email", "") or "").strip().lower())
    if not email:
        logger.warning("user_signed_up: no email for user_id=%s", user.id)
        return

    customer, created = Customer.objects.get_or_create(
        email=email,
        defaults={
            "first_name": (getattr(user, "first_name", "") or "").strip(),
            "last_name": (getattr(user, "last_name", "") or "").strip(),
            "notes": "Auto-created during signup (signal)",
        },
    )

    user_fn = (getattr(user, "first_name", "") or "").strip()
    user_ln = (getattr(user, "last_name", "") or "").strip()
    changed = False
    if user_fn and not (customer.first_name or "").strip():
        customer.first_name = user_fn
        changed = True
    if user_ln and not (customer.last_name or "").strip():
        customer.last_name = user_ln
        changed = True
    if changed:
        customer.save(update_fields=["first_name", "last_name"])

    # Attach reservations for ANY customer row with matching email (case-insensitive)
    updated = TableReservation.objects.filter(
        customer__email__iexact=email,
        created_by__isnull=True,
    ).update(
        created_by=user,
        customer=customer,  # normalize to canonical customer row
    )

    logger.info(
        "user_signed_up: canonical_customer_id=%s (created=%s) attached=%s reservations to user_id=%s",
        customer.id, created, updated, user.id
    )


CACHE_KEY = "no_show_sweep_last_run_date"  # stores YYYY-MM-DD as string


@receiver(user_logged_in)
def run_no_show_sweep_on_staff_login(sender, request, user, **kwargs):
    # Only when app is being used by staff/superuser
    if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
        return

    today = timezone.localdate()
    today_str = today.isoformat()

    # Run at most once per day (across all workers)
    last = cache.get(CACHE_KEY)
    if last == today_str:
        return

    try:
        result = run_no_show_sweep(today=today, ban_threshold=3)
        cache.set(CACHE_KEY, today_str, timeout=60
                  * 60 * 24 * 2)  # 2 days safety
        logger.info(
            "No-show sweep ran on staff login: scanned=%s marked=%s barred=%s",
            result.scanned, result.marked_no_show, result.barred_customers
        )
    except Exception:
        # Never block login because of sweep issues
        logger.exception("No-show sweep failed during staff login")
