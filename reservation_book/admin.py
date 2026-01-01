from django.utils import timezone
from datetime import timedelta
from django.contrib import admin
from .models import TimeSlotAvailability, TableReservation, RestaurantConfig
from .models import Customer


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
        'id',
        'customer_name',          # Custom method
        'customer_email',         # Custom method
        'reservation_date',
        'time_slot',
        'number_of_tables_required_by_patron',
        'is_phone_reservation',
        'reservation_status',
        'created_at',
    )
    list_filter = ('reservation_date', 'time_slot',
                   'is_phone_reservation', 'reservation_status')
    search_fields = ('customer__first_name',
                     'customer__last_name', 'customer__email')
    readonly_fields = ('created_at',)

    def customer_name(self, obj):
        if obj.customer:
            return f"{obj.customer.first_name} {obj.customer.last_name}"
        return "-"
    customer_name.short_description = "Customer Name"

    def customer_email(self, obj):
        if obj.customer:
            return obj.customer.email
        return "-"
    customer_email.short_description = "Email"


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
    fieldsets = (
        (None, {
            'fields': ('first_name', 'last_name', 'email', 'phone', 'mobile')
        }),
        ('Status & Notes', {
            'fields': ('barred', 'notes'),
            'description': "Use 'barred' for customers not welcome. Add notes like 'VIP', 'peanut allergy', 'restaurant critic', etc."
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
