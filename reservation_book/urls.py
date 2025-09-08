from . import views
from django.urls import path
# from django.views.generic import TemplateView

urlpatterns = [
    path("", views.home, name="home"),

    # customer-facing
    path("make_reservation/", views.make_reservation, name="make_reservation"),
    path("reservation_success/", views.reservation_success,
         name="reservation_success"),

    # staff-facing (requires login/permissions in the view)
    path('staff/reservations/', views.reservation_book_view,
         name='reservations'),
    path('staff/reservations/cancel/<int:reservation_id>/',
         views.cancel_reservation, name='cancel_reservation'),
]
