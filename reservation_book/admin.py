from django.utils import timezone
from datetime import timedelta
from django.contrib import admin
from .models import TimeSlotAvailability, TableReservation, RestaurantConfig


class TableReservationInline(admin.TabularInline):
    model = TableReservation
    extra = 0
    fields = ("user", "time_slot",
              "number_of_tables_required_by_patron", "reservation_status")
    readonly_fields = (
        "user", "time_slot", "number_of_tables_required_by_patron", "reservation_status")
    can_delete = False


@admin.register(TimeSlotAvailability)
class TimeSlotAvailabilityAdmin(admin.ModelAdmin):
    list_display = (
        "calendar_date",
        "number_of_tables_available_17_18",
        "number_of_tables_available_18_19",
        "number_of_tables_available_19_20",
        "number_of_tables_available_20_21",
        "number_of_tables_available_21_22",
    )
    list_filter = ("calendar_date",)
    actions = ["update_next_30_days_capacity"]

    def update_next_30_days_capacity(self, request, queryset):
        """Admin action: set all slots for the next 30 days to RestaurantConfig.default_tables_per_slot."""
        config = RestaurantConfig.objects.first()
        if not config:
            self.message_user(
                request, "⚠️ No RestaurantConfig found.", level="error")
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
        "user",
        "first_name",   # directly from TableReservation
        "last_name",    # directly from TableReservation
        "get_calendar_date",
        "time_slot",
        "number_of_tables_required_by_patron",
        "reservation_status",
    )
    list_filter = ("time_slot", "reservation_status")
    search_fields = (
        "user__username",
        "user__email",
        "first_name",    # search by reservation's first_name
        "last_name",     # search by reservation's last_name
    )

    # --- helpers ---
    def get_calendar_date(self, obj):
        return obj.timeslot_availability.calendar_date
    get_calendar_date.admin_order_field = "timeslot_availability__calendar_date"
    get_calendar_date.short_description = "Calendar Date"


@admin.register(RestaurantConfig)
class RestaurantConfigAdmin(admin.ModelAdmin):
    list_display = ("default_tables_per_slot",)
