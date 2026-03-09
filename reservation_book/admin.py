from datetime import timedelta

from django import forms
from django.contrib import admin
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    RestaurantConfig,
    TimeSlotAvailability,
    TableReservation,
    Customer,
)


@admin.register(RestaurantConfig)
class RestaurantConfigAdmin(admin.ModelAdmin):
    list_display = ("default_tables_per_slot",)


@admin.register(TimeSlotAvailability)
class TimeSlotAvailabilityAdmin(admin.ModelAdmin):
    list_display = (
        "calendar_date",
        "number_of_tables_available_17_18",
        "total_cust_demand_for_tables_17_18",
        "number_of_tables_available_18_19",
        "total_cust_demand_for_tables_18_19",
        "number_of_tables_available_19_20",
        "total_cust_demand_for_tables_19_20",
        "number_of_tables_available_20_21",
        "total_cust_demand_for_tables_20_21",
        "number_of_tables_available_21_22",
        "total_cust_demand_for_tables_21_22",
    )
    ordering = ("calendar_date",)

    @admin.action(description="Apply default capacity to next 30 future days")
    def update_next_30_days_capacity(self, request, queryset):
        """
        Apply RestaurantConfig.default_tables_per_slot to TOMORROW onward
        for the next 30 days.

        Important:
        - Past dates are untouched
        - Today is untouched
        - Existing demand is untouched
        - Missing TimeSlotAvailability rows are created first
        """
        config = RestaurantConfig.objects.first()
        if not config:
            self.message_user(
                request,
                "No RestaurantConfig row found.",
                level=messages.ERROR,
            )
            return

        new_capacity = int(config.default_tables_per_slot or 20)

        today = timezone.localdate()
        start_date = today + timedelta(days=1)
        end_date = start_date + timedelta(days=29)

        availability_fields = [
            "number_of_tables_available_17_18",
            "number_of_tables_available_18_19",
            "number_of_tables_available_19_20",
            "number_of_tables_available_20_21",
            "number_of_tables_available_21_22",
        ]

        defaults = {field: new_capacity for field in availability_fields}
        updated_count = 0
        created_count = 0

        for day_offset in range(30):
            target_date = start_date + timedelta(days=day_offset)

            ts, created = TimeSlotAvailability.objects.get_or_create(
                calendar_date=target_date,
                defaults=defaults,
            )

            if created:
                created_count += 1

            changed = False
            for field in availability_fields:
                current = getattr(ts, field, None)
                if current != new_capacity:
                    setattr(ts, field, new_capacity)
                    changed = True

            if changed:
                ts.save(update_fields=availability_fields)
                updated_count += 1

        self.message_user(
            request,
            (
                f"Applied default capacity {new_capacity} to future dates "
                f"{start_date} through {end_date}. "
                f"Created {created_count} row(s), updated \
                    {updated_count} row(s). "
                "Existing demand was left unchanged."
            ),
            level=messages.SUCCESS,
        )

# -----------------------------
# H4-1: Admin validation (LOCKDOWN)
# -----------------------------


class TableReservationAdminForm(forms.ModelForm):
    class Meta:
        model = TableReservation
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        today = timezone.localdate()

        reservation_date = cleaned.get("reservation_date")
        time_slot = (cleaned.get("time_slot") or "").strip()
        customer = cleaned.get("customer")
        timeslot_availability = cleaned.get("timeslot_availability")

        # --- Must have date ---
        if not reservation_date:
            raise ValidationError(
                {"reservation_date": "Reservation date is required."})

        # --- Must not be in the past (admin should not be able
        # to backfill past rows) ---
        if reservation_date < today:
            raise ValidationError(
                {"reservation_date": "Past reservations are not allowed."})

        # --- Must have time slot ---
        if not time_slot:
            raise ValidationError({"time_slot": "Time slot is required."})

        # --- Must have a customer (your business logic expects Customer
        # for analytics/counters/bans) ---
        if not customer:
            raise ValidationError({"customer": "Customer is required."})

        # Require at least an email AND at least one of first/last name
        # (prevents blank identity records)
        cust_email = (getattr(customer, "email", "") or "").strip()
        cust_first = (getattr(customer, "first_name", "") or "").strip()
        cust_last = (getattr(customer, "last_name", "") or "").strip()

        if not cust_email:
            raise ValidationError(
                {"customer": "Customer must have an email address."})
        if not (cust_first or cust_last):
            raise ValidationError(
                {"customer": "Customer must have at \
                 least a first or last name."})

        # --- Optional consistency: if timeslot_availability is set,
        # it must match reservation_date ---
        # (helps prevent admin creating mismatched rows)
        if timeslot_availability and reservation_date:
            # FK uses to_field='calendar_date' in your
            # model; this is still safe.
            avail_date = getattr(timeslot_availability, "calendar_date", None)
            if avail_date and avail_date != reservation_date:
                raise ValidationError(
                    {"timeslot_availability": "TimeSlotAvailability \
                     date must match reservation_date."}
                )

        # --- Sanity checks ---
        tables = cleaned.get("number_of_tables_required_by_patron")
        if tables is None or int(tables) < 1:
            raise ValidationError(
                {"number_of_tables_required_by_patron":
                 "Tables must be at least 1."})

        duration = cleaned.get("duration_hours")
        if duration is None or int(duration) < 1:
            raise ValidationError(
                {"duration_hours": "Duration must be at least 1 hour."})

        return cleaned


@admin.register(TableReservation)
class TableReservationAdmin(admin.ModelAdmin):
    form = TableReservationAdminForm

    list_display = (
        "id",
        "customer_name",
        "customer_email",
        "reservation_date",
        "time_slot_display",
        "duration_hours",
        "number_of_tables_required_by_patron",
        "is_phone_reservation",
        "status_badge",
        "created_at",
    )

    list_filter = (
        "reservation_date",
        "time_slot",
        "is_phone_reservation",
        "status",
    )

    search_fields = (
        "customer__first_name",
        "customer__last_name",
        "customer__email",
    )

    readonly_fields = ("created_at", "updated_at")
    ordering = ("-reservation_date", "time_slot", "id")

    def customer_name(self, obj):
        if obj.customer:
            return f"{obj.customer.first_name} {obj.customer.last_name}"\
                .strip() or "-"
        return "-"
    customer_name.short_description = "Customer Name"

    def customer_email(self, obj):
        return obj.customer.email if obj.customer else "-"
    customer_email.short_description = "Email"

    def time_slot_display(self, obj):
        return obj.time_range_pretty
    time_slot_display.short_description = "Time Slot"

    def status_badge(self, obj):
        status = getattr(obj, "status", "") or ""
        # Prefer the new status system; fall back to legacy boolean if needed.
        if status == getattr(TableReservation, "STATUS_NO_SHOW", "no_show"):
            return format_html('<span class="badge bg-danger">No-show</span>')
        if status == getattr(
                TableReservation, "STATUS_COMPLETED", "completed"):
            return format_html(
                '<span class="badge bg-primary">Completed</span>')
        if status == getattr(TableReservation,
                             "STATUS_CANCELLED", "cancelled"):
            return format_html(
                '<span class="badge bg-secondary">Cancelled</span>')
        if status == getattr(TableReservation, "STATUS_ACTIVE", "active"):
            return format_html('<span class="badge bg-success">Active</span>')

        # legacy fallback
        if getattr(obj, "reservation_status", False):
            return format_html('<span class="badge bg-success">Active</span>')
        return format_html('<span class="badge bg-secondary">Inactive</span>')
    status_badge.short_description = "Status"


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = (
        "first_name",
        "last_name",
        "email",
        "phone",
        "mobile",
        "barred",
        "created_at",
    )
    list_filter = ("barred", "created_at")
    search_fields = ("first_name", "last_name", "email",
                     "phone", "mobile", "notes")
    readonly_fields = ("created_at", "updated_at")
