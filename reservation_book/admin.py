from django.utils import timezone
from datetime import timedelta
from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from django.db import transaction
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


class ReservationScopeFilter(admin.SimpleListFilter):
    """
    Default admin scope = ACTIVE reservations only.
    Staff/owners can switch to 'All' for analytics.
    """
    title = _("Scope")
    parameter_name = "scope"

    def lookups(self, request, model_admin):
        return (
            ("active", _("Active only")),
            ("all", _("All (including cancelled/history)")),
        )

    def queryset(self, request, queryset):
        val = self.value() or "active"
        if val == "all":
            return queryset
        # Default: active
        if hasattr(TableReservation, "status"):
            return queryset.filter(status=TableReservation.STATUS_ACTIVE)
        if hasattr(TableReservation, "reservation_status"):
            return queryset.filter(reservation_status=True)
        return queryset


@admin.register(TableReservation)
class TableReservationAdmin(admin.ModelAdmin):
    """
    Admin defaults to ACTIVE reservations only.
    Cancelled/historical reservations remain in DB for trend analysis,
    but are hidden from daily ops unless you switch Scope -> "All".
    """

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
        ReservationScopeFilter,  # IMPORTANT: first, so it's easy to find
        "reservation_date",
        "time_slot",
        "is_phone_reservation",
    )

    search_fields = (
        "customer__first_name",
        "customer__last_name",
        "customer__email",
    )

    readonly_fields = ("created_at", "updated_at")
    ordering = ("reservation_date", "time_slot", "id")

    actions = (
        "action_mark_completed",
        "action_mark_no_show",
        "action_mark_cancelled_soft",
    )

    # ----------------------------
    # Default queryset behavior
    # ----------------------------
    def get_queryset(self, request):
        """
        Default admin list view shows ACTIVE only.
        The Scope filter can override this.
        """
        qs = super().get_queryset(request).select_related(
            "customer", "timeslot_availability"
        )

        # If the user has explicitly selected a scope, do not override.
        # (Admin will apply our ReservationScopeFilter queryset.)
        # NOTE: check for None specifically so an explicit scope value always wins.
        if request.GET.get("scope") is not None:
            return qs

        # Default: active only
        if hasattr(TableReservation, "status"):
            return qs.filter(status=TableReservation.STATUS_ACTIVE)
        if hasattr(TableReservation, "reservation_status"):
            return qs.filter(reservation_status=True)
        return qs

    # ----------------------------
    # Column helpers
    # ----------------------------
    def customer_name(self, obj):
        if obj.customer:
            first = (getattr(obj.customer, "first_name", "") or "").strip()
            last = (getattr(obj.customer, "last_name", "") or "").strip()
            name = (first + " " + last).strip()
            return name or "-"
        return "-"
    customer_name.short_description = "Customer Name"

    def customer_email(self, obj):
        if obj.customer and getattr(obj.customer, "email", None):
            return obj.customer.email
        return "-"
    customer_email.short_description = "Email"

    def status_badge(self, obj):
        """
        Show status from the new 'status' field if present;
        fall back to legacy boolean.
        """
        if hasattr(obj, "status"):
            return getattr(obj, "status", "-") or "-"
        if hasattr(obj, "reservation_status"):
            return "active" if obj.reservation_status else "cancelled"
        return "-"
    status_badge.short_description = "Status"

    def time_slot_display(self, obj):
        """
        Displays the true time range when duration_hours > 1.

        IMPORTANT:
        Do NOT depend on a view import here.
        Use model-level slot constants if you defined them in models.py.
        If not present, falls back safely.
        """
        from . import models as model_module

        SLOT_LABELS = getattr(model_module, "SLOT_LABELS", {})
        SLOT_KEYS = getattr(model_module, "SLOT_KEYS",
                            list(SLOT_LABELS.keys()))

        start_slot = getattr(obj, "time_slot", None)
        if not start_slot:
            return "-"

        start_label = SLOT_LABELS.get(start_slot, start_slot)

        try:
            dur = int(getattr(obj, "duration_hours", 1) or 1)
        except Exception:
            dur = 1
        if dur <= 1:
            return start_label

        if start_slot in SLOT_KEYS:
            start_index = SLOT_KEYS.index(start_slot)
            end_index = min(start_index + dur - 1, len(SLOT_KEYS) - 1)
            end_slot = SLOT_KEYS[end_index]
            end_label = SLOT_LABELS.get(end_slot, end_slot)

            try:
                start_time = start_label.split("–")[0].strip()
                end_time = end_label.split("–")[1].strip()
                return f"{start_time}–{end_time}"
            except Exception:
                return start_label

        return start_label
    time_slot_display.short_description = "Time Slot"

    # ----------------------------
    # Admin actions (no deletes)
    # ----------------------------
    def action_mark_completed(self, request, queryset):
        """
        Mark selected reservations as COMPLETED.
        (No availability math changes; completed reservations stay in history.)

        IMPORTANT:
        If legacy reservation_status exists anywhere, flip it False so these
        stop appearing in any old "active" filters.
        """
        if not hasattr(TableReservation, "status"):
            self.message_user(
                request, "Status field not available on this model.", level="error"
            )
            return

        now = timezone.now()

        # Keep legacy boolean consistent if present
        if hasattr(TableReservation, "reservation_status"):
            updated = queryset.update(
                status=TableReservation.STATUS_COMPLETED,
                completed_at=now,
                reservation_status=False,
            )
        else:
            updated = queryset.update(
                status=TableReservation.STATUS_COMPLETED,
                completed_at=now,
            )

        self.message_user(
            request, f"Marked {updated} reservation(s) as completed.")
    action_mark_completed.short_description = "Mark selected as completed"

    def action_mark_no_show(self, request, queryset):
        """
        Mark selected reservations as NO-SHOW.
        (No availability math changes; no-show reservations stay in history.)

        IMPORTANT:
        If legacy reservation_status exists anywhere, flip it False so these
        stop appearing in any old "active" filters.
        """
        if not hasattr(TableReservation, "status"):
            self.message_user(
                request, "Status field not available on this model.", level="error"
            )
            return

        if hasattr(TableReservation, "reservation_status"):
            updated = queryset.update(
                status=TableReservation.STATUS_NO_SHOW,
                reservation_status=False,
            )
        else:
            updated = queryset.update(status=TableReservation.STATUS_NO_SHOW)

        self.message_user(
            request, f"Marked {updated} reservation(s) as no-show.")
    action_mark_no_show.short_description = "Mark selected as no-show"

    def action_mark_cancelled_soft(self, request, queryset):
        """
        Soft-cancel selected reservations and release demand back to availability.

        This is the admin-safe alternative to deleting, so you keep history.
        """
        if not queryset.exists():
            return

        count = 0
        for r in queryset.select_related("timeslot_availability"):
            # Skip already-cancelled (new) or already-inactive (legacy)
            if hasattr(r, "status") and getattr(r, "status", None) == TableReservation.STATUS_CANCELLED:
                continue
            if hasattr(r, "reservation_status") and r.reservation_status is False:
                # still ensure status is cancelled if status field exists
                if hasattr(r, "status") and r.status != TableReservation.STATUS_CANCELLED:
                    r.status = TableReservation.STATUS_CANCELLED
                    r.cancelled_at = timezone.now()
                    r.save(update_fields=["status", "cancelled_at"])
                continue

            # Release demand across duration_hours slots
            self._release_demand_for_reservation(r)

            # Prefer model lifecycle helper if available (keeps one truth)
            update_fields = []
            if hasattr(r, "mark_cancelled"):
                r.mark_cancelled()
                # mark_cancelled should set status/cancelled_at and legacy boolean if present
                if hasattr(r, "status"):
                    update_fields.append("status")
                if hasattr(r, "cancelled_at"):
                    update_fields.append("cancelled_at")
                if hasattr(r, "reservation_status"):
                    update_fields.append("reservation_status")
            else:
                # Fallback (older model)
                if hasattr(r, "status"):
                    r.status = TableReservation.STATUS_CANCELLED
                    r.cancelled_at = timezone.now()
                    update_fields.extend(["status", "cancelled_at"])
                if hasattr(r, "reservation_status"):
                    r.reservation_status = False
                    update_fields.append("reservation_status")

            if update_fields:
                r.save(update_fields=update_fields)
            else:
                r.save()

            count += 1

        self.message_user(
            request, f"Cancelled {count} reservation(s) and released demand.")
    action_mark_cancelled_soft.short_description = "Cancel selected (soft) + release demand"

    # ----------------------------
    # Delete behavior (keep your old behavior, but multi-hour safe)
    # ----------------------------
    def delete_queryset(self, request, queryset):
        """
        If you bulk-delete from admin, release demand FIRST.

        WARNING: Deleting removes history. Prefer "Cancel selected (soft)" above.
        """
        for reservation in queryset.select_related("timeslot_availability"):
            self._release_demand_for_reservation(reservation)
        queryset.delete()

    def delete_model(self, request, obj):
        """
        Single delete also releases demand.
        """
        self._release_demand_for_reservation(obj)
        super().delete_model(request, obj)

    def _release_demand_for_reservation(self, reservation: TableReservation):
        """
        Release demand back to TimeSlotAvailability across duration_hours slots.
        Safe + atomic (locks the TSA row).
        """
        ts = getattr(reservation, "timeslot_availability", None)
        if not ts:
            return

        from . import models as model_module
        SLOT_LABELS = getattr(model_module, "SLOT_LABELS", {})
        SLOT_KEYS = getattr(model_module, "SLOT_KEYS",
                            list(SLOT_LABELS.keys()))

        start_slot = getattr(reservation, "time_slot", None)
        if not start_slot:
            return

        try:
            tables_needed = int(
                getattr(reservation, "number_of_tables_required_by_patron", 0) or 0)
        except Exception:
            tables_needed = 0

        try:
            duration = int(getattr(reservation, "duration_hours", 1) or 1)
        except Exception:
            duration = 1
        duration = max(duration, 1)

        if start_slot in SLOT_KEYS:
            start_index = SLOT_KEYS.index(start_slot)
            affected_slots = SLOT_KEYS[start_index: start_index + duration]
        else:
            affected_slots = [start_slot]

        with transaction.atomic():
            ts_locked = TimeSlotAvailability.objects.select_for_update().get(pk=ts.pk)
            update_fields = []
            for slot in affected_slots:
                demand_field = f"total_cust_demand_for_tables_{slot}"
                current = getattr(ts_locked, demand_field, 0) or 0
                new_val = max(0, int(current) - tables_needed)
                setattr(ts_locked, demand_field, new_val)
                update_fields.append(demand_field)

            if update_fields:
                ts_locked.save(update_fields=update_fields)

    # OPTIONAL: If you want to prevent deletes entirely, uncomment this and
    # replace queryset.delete() with self._soft_cancel_queryset(queryset)
    #
    # def _soft_cancel_queryset(self, queryset):
    #     for r in queryset:
    #         # release demand
    #         self._release_demand_for_reservation(r)
    #         # mark cancelled (new status or legacy boolean)
    #         if hasattr(r, "status") and hasattr(TableReservation, "STATUS_CANCELLED"):
    #             r.status = TableReservation.STATUS_CANCELLED
    #             r.save(update_fields=["status"])
    #         elif hasattr(r, "reservation_status"):
    #             r.reservation_status = False
    #             r.save(update_fields=["reservation_status"])


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
