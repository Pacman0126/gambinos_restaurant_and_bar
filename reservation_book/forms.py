from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.db import transaction
from allauth.account.forms import SignupForm
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


class CustomerSignupForm(SignupForm):
    """
    Website signup requirements:
    - username required (per your requirement)
    - email required (for confirmations)
    - first/last required (for Customer DB + nicer emails)
    - ensure Customer exists keyed by email (Customer has no user FK)
    """
    first_name = forms.CharField(
        max_length=150, required=True, label="First name")
    last_name = forms.CharField(
        max_length=150, required=True, label="Last name")

    @transaction.atomic
    def save(self, request):
        user = super().save(request)

        # Persist names to User (useful for admin + emails)
        user.first_name = self.cleaned_data["first_name"].strip()
        user.last_name = self.cleaned_data["last_name"].strip()
        user.save()

        # Email is required: use it as Customer key
        email = (user.email or "").strip().lower()
        if not email:
            # Defensive: should not happen with ACCOUNT_EMAIL_REQUIRED=True
            raise ValueError(
                "Email is required for signup (needed for confirmations).")

        Customer.objects.update_or_create(
            email=email,
            defaults={
                "first_name": user.first_name,
                "last_name": user.last_name,
            },
        )

        return user


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

    # ✅ Ensure tables defaults to 1 (form-level)
    number_of_tables_required_by_patron = forms.IntegerField(
        min_value=1,
        initial=1,
        label="Tables",
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1}),
    )

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
            "duration_hours": forms.Select(attrs={"class": "form-select"}),
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

        if commit:
            reservation.save()
        return reservation


class EditReservationForm(forms.ModelForm):
    class Meta:
        model = TableReservation
        fields = [
            'duration_hours',
            'number_of_tables_required_by_patron',
        ]
        widgets = {
            'duration_hours': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '1',
                'max': '5',
                'step': '1',
            }),
            'number_of_tables_required_by_patron': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '1',
                'step': '1',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Optional: make fields required or add help text if needed
        self.fields['duration_hours'].required = True
        self.fields['number_of_tables_required_by_patron'].required = True

        # Pre-fill from instance if needed (already handled by instance=reservation)
        if self.instance and self.instance.pk:
            self.initial['duration_hours'] = self.instance.duration_hours or 1
            self.initial['number_of_tables_required_by_patron'] = self.instance.number_of_tables_required_by_patron or 1

        # Optional: disable or hide if you want read-only customer info in form (but better in left card)
        # self.fields['duration_hours'].disabled = True  # example
