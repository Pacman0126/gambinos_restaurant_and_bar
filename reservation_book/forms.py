from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
# from .models import ReservationBook   # no longer needed
from .models import TimeSlotAvailability, TableReservation


class ReservationForm(forms.Form):
    reservation_date = forms.ModelChoiceField(
        queryset=TimeSlotAvailability.objects.all(),
        to_field_name="calendar_date",
        label="Choose a date"
    )
    time_slot = forms.ChoiceField(label="Choose a time slot")
    number_of_tables_required_by_patron = forms.IntegerField(
        min_value=1, label="Number of tables"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if "reservation_date" in self.data:
            try:
                date_id = self.data.get("reservation_date")
                if date_id:
                    ts = TimeSlotAvailability.objects.get(
                        calendar_date=date_id)
                    slot_map = {
                        "17_18": ("17:00 - 18:00", ts.total_cust_demand_for_tables_17_18, ts.number_of_tables_available_17_18),
                        "18_19": ("18:00 - 19:00", ts.total_cust_demand_for_tables_18_19, ts.number_of_tables_available_18_19),
                        "19_20": ("19:00 - 20:00", ts.total_cust_demand_for_tables_19_20, ts.number_of_tables_available_19_20),
                        "20_21": ("20:00 - 21:00", ts.total_cust_demand_for_tables_20_21, ts.number_of_tables_available_20_21),
                        "21_22": ("21:00 - 22:00", ts.total_cust_demand_for_tables_21_22, ts.number_of_tables_available_21_22),
                    }
                    available_slots = [
                        (key, label) for key, (label, demand, available) in slot_map.items()
                        if demand < available
                    ]
                    self.fields["time_slot"].choices = available_slots
            except TimeSlotAvailability.DoesNotExist:
                pass
        else:
            # Default: allow all slots (useful when form first loads)
            self.fields["time_slot"].choices = [
                ("17_18", "17:00 - 18:00"),
                ("18_19", "18:00 - 19:00"),
                ("19_20", "19:00 - 20:00"),
                ("20_21", "20:00 - 21:00"),
                ("21_22", "21:00 - 22:00"),
            ]


class SignUpForm(UserCreationForm):
    email = forms.EmailField(required=True)

    class Meta:
        model = User
        fields = ["username", "email", "password1", "password2"]
