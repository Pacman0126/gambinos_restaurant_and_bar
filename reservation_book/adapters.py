# reservation_book/adapters.py
from allauth.account.adapter import DefaultAccountAdapter
from django.urls import reverse


class CustomAccountAdapter(DefaultAccountAdapter):
    def get_login_redirect_url(self, request):
        user = request.user
        if user.is_superuser:
            return reverse('staff_management')
        elif user.is_staff:
            if user.username == user.email:  # First login if username still = email
                return reverse('first_login_setup')
            return reverse('staff_dashboard')
        else:
            return reverse('make_reservation')
