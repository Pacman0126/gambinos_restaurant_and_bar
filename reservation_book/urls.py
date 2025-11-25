from django.urls import path
from . import views

urlpatterns = [
    # Customer-facing / public views
    path("", views.home, name="home"),
    path("my_reservations/", views.my_reservations, name="my_reservations"),
    path("make_reservation/", views.make_reservation, name="make_reservation"),
    path(
        "cancel_reservation/<int:reservation_id>/",
        views.cancel_reservation,
        name="cancel_reservation",
    ),
    path("menu/", views.menu, name="menu"),

    # Staff tools (matching navbar)
    path(
        "staff/dashboard/",
        views.user_reservations_overview,
        name="user_reservations_overview",
    ),
    path(
        "staff/customer/<int:user_id>/history/",
        views.user_reservation_history,
        name="user_reservation_history",
    ),
    path(
        "staff/phone-reservation/",
        views.create_phone_reservation,
        name="create_phone_reservation",
    ),
    path(
        "reservations/<int:reservation_id>/edit/",
        views.update_reservation,
        name="update_reservation",
    ),

]
