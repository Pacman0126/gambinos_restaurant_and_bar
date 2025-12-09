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


# ----------------------------------------------------------------------
# Staff-only phone reservation form
# ----------------------------------------------------------------------

# Choices mirror your SLOT_LABELS keys/labels from views.py
TIME_SLOT_CHOICES = [
    ("17_18", "17:00-18:00"),
    ("18_19", "18:00-19:00"),
    ("19_20", "19:00-20:00"),
    ("20_21", "20:00-21:00"),
    ("21_22", "21:00-22:00"),
]


class PhoneReservationForm(forms.ModelForm):
    """
    Used by staff when taking a reservation over the phone.

    Email is optional, but if provided we:
    - send a confirmation email
    - link the reservation to an existing user with that email OR
    - create a lightweight account for that email (handled in the view)
    """

    email = forms.EmailField(
        required=False,
        label="Email address",
        help_text=(
            "Optional, but required if the guest wants a confirmation email "
            "and online access to their reservations."
        ),
    )

    class Meta:
        model = TableReservation
        fields = [
            "first_name",
            "last_name",
            "email",
            "phone",
            "mobile",
            "number_of_tables_required_by_patron",
        ]

        widgets = {
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "mobile": forms.TextInput(attrs={"class": "form-control"}),
            "number_of_tables_required_by_patron": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "min": 1,
                }
            ),
        }


class EditReservationForm(forms.ModelForm):
    reservation_date = forms.DateField(
        widget=forms.DateInput(
            attrs={"type": "date", "class": "form-control"}),
        label="Reservation Date",
    )

    time_slot = forms.ChoiceField(
        choices=TIME_SLOT_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Time Slot",
    )

    class Meta:
        model = TableReservation
        fields = ["reservation_date", "time_slot",
                  "number_of_tables_required_by_patron"]
        widgets = {
            "number_of_tables_required_by_patron": forms.NumberInput(
                attrs={"class": "form-control", "min": 1}
            ),
        }
