from . import views
from django.urls import path

urlpatterns = [
    path('', views.ReservationDisplay.as_view(), name='home'),
]
