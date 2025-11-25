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
    Staff-facing form to capture phone-in reservations.

    Adds an optional email field so we can:
    - link to an existing online customer (if email matches a User)
    - or send a confirmation + signup invitation to new customers.
    """

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

    first_name = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={"class": "form-control"}),
        label="First Name",
    )
    last_name = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={"class": "form-control"}),
        label="Last Name",
    )

    phone = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
        label="Phone (Landline)",
    )

    mobile = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
        label="Mobile",
    )

    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={"class": "form-control"}),
        label="Customer Email (optional)",
        help_text="If this matches an online account, the reservation will appear in their history.",
    )

    class Meta:
        model = TableReservation
        fields = [
            "reservation_date",
            "time_slot",
            "number_of_tables_required_by_patron",
            "first_name",
            "last_name",
            "phone",
            "mobile",
            "email",
        ]
        widgets = {
            "number_of_tables_required_by_patron": forms.NumberInput(
                attrs={"class": "form-control", "min": 1}
            ),
        }

    def __init__(self, *args, **kwargs):
        """
        Optionally accept for_date to show 'X tables free' for each time slot.
        """
        self.for_date = kwargs.pop("for_date", None)
        super().__init__(*args, **kwargs)

        # Human-friendly labels + (optional) availability numbers later
        base_choices = [(key, label) for key, label in TIME_SLOT_CHOICES]

        # If we have a date, decorate labels with availability
        if self.for_date:
            try:
                ts = TimeSlotAvailability.objects.get(
                    calendar_date=self.for_date)
            except TimeSlotAvailability.DoesNotExist:
                ts = None

            decorated = []
            for key, label in base_choices:
                display_label = label
                if ts:
                    avail_field = f"number_of_tables_available_{key}"
                    demand_field = f"total_cust_demand_for_tables_{key}"
                    slot_available = getattr(ts, avail_field, 0) or 0
                    slot_demand = getattr(ts, demand_field, 0) or 0
                    free_tables = max(slot_available - slot_demand, 0)
                    display_label = f"{label} ({free_tables} tables free)"
                decorated.append((key, display_label))
            self.fields["time_slot"].choices = decorated
        else:
            self.fields["time_slot"].choices = base_choices


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
