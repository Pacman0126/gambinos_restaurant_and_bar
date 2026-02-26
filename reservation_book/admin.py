from datetime import timedelta

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from django.utils.html import format_html

from .models import TimeSlotAvailability, TableReservation, RestaurantConfig, Customer


@admin.register(TimeSlotAvailability)
class TimeSlotAvailabilityAdmin(admin.ModelAdmin):
    list_display = (
        "calendar_date",
        "remaining_17_18",
        "remaining_18_19",
        "remaining_19_20",
        "remaining_20_21",
        "remaining_21_22",
    )
    list_filter = ("calendar_date",)
    actions = ["update_next_30_days_capacity"]

    # Real remaining columns
    def remaining_17_18(self, obj):
        return obj.left_for("17_18")
    remaining_17_18.short_description = "17:00–18:00 (Rem)"

    def remaining_18_19(self, obj):
        return obj.left_for("18_19")
    remaining_18_19.short_description = "18:00–19:00 (Rem)"

    def remaining_19_20(self, obj):
        return obj.left_for("19_20")
    remaining_19_20.short_description = "19:00–20:00 (Rem)"

    def remaining_20_21(self, obj):
        return obj.left_for("20_21")
    remaining_20_21.short_description = "20:00–21:00 (Rem)"

    def remaining_21_22(self, obj):
        return obj.left_for("21_22")
    remaining_21_22.short_description = "21:00–22:00 (Rem)"

    def update_next_30_days_capacity(self, request, queryset):
        config = RestaurantConfig.objects.first()
        if not config:
            self.message_user(
                request, "No RestaurantConfig found.", level="error")
            return

        today = timezone.now().date()
        end_date = today + timedelta(days=30)

        updated = TimeSlotAvailability.objects.filter(
            calendar_date__gte=today,
            calendar_date__lte=end_date,
        ).update(
            number_of_tables_available_17_18=config.default_tables_per_slot,
            number_of_tables_available_18_19=config.default_tables_per_slot,
            number_of_tables_available_19_20=config.default_tables_per_slot,
            number_of_tables_available_20_21=config.default_tables_per_slot,
            number_of_tables_available_21_22=config.default_tables_per_slot,
        )

        self.message_user(
            request,
            f"Updated capacity for {updated} day(s) to {config.default_tables_per_slot} tables per slot.",
        )
    update_next_30_days_capacity.short_description = "Update next 30 days to RestaurantConfig capacity"


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

        # --- Must not be in the past (admin should not be able to backfill past rows) ---
        if reservation_date < today:
            raise ValidationError(
                {"reservation_date": "Past reservations are not allowed."})

        # --- Must have time slot ---
        if not time_slot:
            raise ValidationError({"time_slot": "Time slot is required."})

        # --- Must have a customer (your business logic expects Customer for analytics/counters/bans) ---
        if not customer:
            raise ValidationError({"customer": "Customer is required."})

        # Require at least an email AND at least one of first/last name (prevents blank identity records)
        cust_email = (getattr(customer, "email", "") or "").strip()
        cust_first = (getattr(customer, "first_name", "") or "").strip()
        cust_last = (getattr(customer, "last_name", "") or "").strip()

        if not cust_email:
            raise ValidationError(
                {"customer": "Customer must have an email address."})
        if not (cust_first or cust_last):
            raise ValidationError(
                {"customer": "Customer must have at least a first or last name."})

        # --- Optional consistency: if timeslot_availability is set, it must match reservation_date ---
        # (helps prevent admin creating mismatched rows)
        if timeslot_availability and reservation_date:
            # FK uses to_field='calendar_date' in your model; this is still safe.
            avail_date = getattr(timeslot_availability, "calendar_date", None)
            if avail_date and avail_date != reservation_date:
                raise ValidationError(
                    {"timeslot_availability": "TimeSlotAvailability date must match reservation_date."}
                )

        # --- Sanity checks ---
        tables = cleaned.get("number_of_tables_required_by_patron")
        if tables is None or int(tables) < 1:
            raise ValidationError(
                {"number_of_tables_required_by_patron": "Tables must be at least 1."})

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
            return f"{obj.customer.first_name} {obj.customer.last_name}".strip() or "-"
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
        if status == getattr(TableReservation, "STATUS_COMPLETED", "completed"):
            return format_html('<span class="badge bg-primary">Completed</span>')
        if status == getattr(TableReservation, "STATUS_CANCELLED", "cancelled"):
            return format_html('<span class="badge bg-secondary">Cancelled</span>')
        if status == getattr(TableReservation, "STATUS_ACTIVE", "active"):
            return format_html('<span class="badge bg-success">Active</span>')

        # legacy fallback
        if getattr(obj, "reservation_status", False):
            return format_html('<span class="badge bg-success">Active</span>')
        return format_html('<span class="badge bg-secondary">Inactive</span>')
    status_badge.short_description = "Status"


@admin.register(RestaurantConfig)
class RestaurantConfigAdmin(admin.ModelAdmin):
    list_display = ("default_tables_per_slot",)


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
