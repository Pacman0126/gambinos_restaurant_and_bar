# reservation_book/urls.py

from django.urls import path
from . import views

urlpatterns = [
    # Public site
    path("", views.home, name="home"),
    path("menu/", views.menu, name="menu"),

    # Customer reservation flows
    path("reserve/", views.make_reservation, name="make_reservation"),
    path("my_reservations/", views.my_reservations, name="my_reservations"),
    path(
        "reservation/<int:reservation_id>/cancel/",
        views.cancel_reservation,
        name="cancel_reservation",
    ),
    path(
        "reservation/<int:reservation_id>/edit/",
        views.update_reservation,
        name="update_reservation",
    ),

    # Staff dashboard + tools
    path(
        "staff/dashboard/",
        views.staff_dashboard,
        name="staff_dashboard",
    ),
    path(
        "staff/reservations/",
        views.staff_reservations,
        name="staff_reservations",
    ),
    path(
        "staff/customers/",
        views.user_reservations_overview,
        name="user_reservations_overview",
    ),
    path(
        "staff/customer/<int:user_id>/history/",
        views.user_reservation_history,
        name="user_reservation_history",
    ),
    path("staff/phone-reservation/",
         views.make_reservation,
         name="create_phone_reservation"),

    path(
        "ajax/lookup-customer/",
        views.ajax_lookup_customer,
        name="ajax_lookup_customer",
    ),
]
