from django.urls import path, include
from reservation_book import views   # explicitly import from your app
from reservation_book.views import signup

urlpatterns = [
    path("", views.home, name="home"),
    path("my_reservations/", views.my_reservations, name="my_reservations"),
    # authentication (login/logout/password reset)
    path("accounts/", include("django.contrib.auth.urls")),
    path("accounts/signup/", views.signup, name="signup"),

    # customer-facing
    path("cancel_reservation/<int:reservation_id>/",
         views.cancel_reservation, name="cancel_reservation"),
    path("make_reservation/", views.make_reservation, name="make_reservation"),
    path("menu/", views.menu, name="menu"),

]
