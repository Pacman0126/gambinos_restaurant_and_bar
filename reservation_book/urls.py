from django.urls import path, include
from reservation_book import views   # explicitly import from your app

urlpatterns = [
    path("", views.home, name="home"),

    # customer-facing
    path("make_reservation/", views.make_reservation, name="make_reservation"),

    # authentication (login/logout/password reset)
    path("accounts/", include("django.contrib.auth.urls")),
    path("accounts/signup/", views.signup, name="signup"),
]
