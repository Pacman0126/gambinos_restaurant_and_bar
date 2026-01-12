from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
# from .models import ReservationBook   # no longer needed
from .models import TimeSlotAvailability, TableReservation, Customer


# class ReservationForm(forms.Form):
#     reservation_date = forms.ModelChoiceField(
#         queryset=TimeSlotAvailability.objects.all(),
#         to_field_name="calendar_date",
#         label="Choose a date"
#     )
#     time_slot = forms.ChoiceField(label="Choose a time slot")
#     number_of_tables_required_by_patron = forms.IntegerField(
#         min_value=1, label="Number of tables"
#     )


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
    """Staff form for phone-in reservations.

    Important: We do NOT write Customer to the DB in the form.
    The view is responsible for upserting Customer and for all capacity checks.
    """

    # Customer fields (not part of TableReservation model)
    first_name = forms.CharField(
        max_length=100, required=True, label="First Name")
    last_name = forms.CharField(
        max_length=100, required=True, label="Last Name")
    email = forms.EmailField(required=True, label="Email Address")
    phone = forms.CharField(max_length=20, required=False, label="Phone")
    mobile = forms.CharField(max_length=20, required=False, label="Mobile")

    # Booking extensions
    until_close = forms.BooleanField(
        required=False,
        label="Book from selected start until kitchen close",
        help_text="If checked, duration will be auto-set to the last available slot of the day.",
    )
    series_days = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=14,
        initial=1,
        label="Consecutive days (series)",
        help_text="For conferences: book the same time block for N consecutive days (including the start date).",
    )

    class Meta:
        model = TableReservation
        fields = [
            "reservation_date",
            "time_slot",
            "duration_hours",
            "number_of_tables_required_by_patron",
            "timeslot_availability",
        ]
        widgets = {
            "reservation_date": forms.HiddenInput(),
            "time_slot": forms.HiddenInput(),
            "timeslot_availability": forms.HiddenInput(),
        }

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            raise forms.ValidationError("Email is required.")
        return email

    def clean_series_days(self):
        val = self.cleaned_data.get("series_days")
        return 1 if not val else int(val)

    def save(self, commit=True):
        """Return an unsaved reservation with an *unsaved* Customer attached.

        The view will:
          - upsert Customer by email
          - create/reuse the auth User
          - enforce capacity + update demand
          - save one or more reservations
        """
        reservation = super().save(commit=False)

        customer = Customer(
            first_name=(self.cleaned_data.get("first_name") or "").strip(),
            last_name=(self.cleaned_data.get("last_name") or "").strip(),
            email=(self.cleaned_data.get("email") or "").strip().lower(),
            phone=(self.cleaned_data.get("phone") or "").strip(),
            mobile=(self.cleaned_data.get("mobile") or "").strip(),
        )
        reservation.customer = customer

        # Do not save here; view decides if/how many reservations exist.
        if commit:
            reservation.save()
        return reservation


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
        fields = [
            'reservation_date',
            'time_slot',
            'number_of_tables_required_by_patron',
        ]
        widgets = {
            "number_of_tables_required_by_patron": forms.NumberInput(
                attrs={"class": "form-control", "min": 1}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.customer:
            # Pre-fill customer details in form (read-only or editable as needed)
            self.fields['customer_first_name'] = forms.CharField(
                initial=self.instance.customer.first_name,
                label="First Name",
                # disabled=True  # or remove disabled to allow edit
            )
            self.fields['customer_last_name'] = forms.CharField(
                initial=self.instance.customer.last_name,
                label="Last Name",
                # disabled=True
            )
            self.fields['customer_email'] = forms.EmailField(
                initial=self.instance.customer.email,
                label="Email",
                # disabled=True
            )
            self.fields['customer_phone'] = forms.CharField(
                initial=self.instance.customer.phone or '',
                label="Phone",
                required=False,
                # disabled=True
            )
            self.fields['customer_mobile'] = forms.CharField(
                initial=self.instance.customer.mobile or '',
                label="Mobile",
                required=False,
                # disabled=True
            )
