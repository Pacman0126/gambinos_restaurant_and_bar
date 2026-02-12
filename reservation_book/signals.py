import logging
from django.dispatch import receiver
from allauth.account.signals import user_signed_up
from .models import TableReservation, Customer


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
