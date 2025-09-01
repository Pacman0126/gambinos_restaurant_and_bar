from django import forms
from .models import ReservationBook


class ReservationsForm(forms.ModelForm):
    class Meta:
        model = ReservationBook
        fields = ['reservation_id', 'first_name']
