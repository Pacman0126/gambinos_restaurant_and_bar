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


class SignUpForm(UserCreationForm):
    email = forms.EmailField(required=True)

    class Meta:
        model = User
        fields = ["username", "email", "password1", "password2"]
