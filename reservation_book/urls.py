from . import views
from django.urls import path
from django.views.generic import TemplateView
from .views import home, reservations, make_reservation

urlpatterns = [
    path('', home, name='home'),
    path('reservation_book/', make_reservation, name='make_reservation'),
    path('reservation_book/', reservations, name='reservations'),
    path("reservation_success/", views.reservation_success,
         name="reservation_success"),
    path(
        "zoho-domain-verification.html",
        TemplateView.as_view(
            template_name="reservation_book/zoho-domain-verification.html"
        ),
    ),

]
