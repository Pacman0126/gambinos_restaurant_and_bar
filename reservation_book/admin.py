from django.utils import timezone
from datetime import timedelta
from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
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


@admin.register(TableReservation)
class TableReservationAdmin(admin.ModelAdmin):
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

    # Define missing methods (fixes E108)
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
        if obj.status == 'active' or getattr(obj, 'reservation_status', False):
            return format_html('<span class="badge bg-success">Active</span>')
        else:
            return format_html('<span class="badge bg-secondary">Inactive</span>')
    status_badge.short_description = "Status"
    status_badge.allow_tags = True   # for older Django versions


@admin.register(RestaurantConfig)
class RestaurantConfigAdmin(admin.ModelAdmin):
    list_display = ("default_tables_per_slot",)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('first_name', 'last_name', 'email',
                    'phone', 'mobile', 'barred', 'created_at')
    list_filter = ('barred', 'created_at')
    search_fields = ('first_name', 'last_name', 'email',
                     'phone', 'mobile', 'notes')
    readonly_fields = ('created_at', 'updated_at')
