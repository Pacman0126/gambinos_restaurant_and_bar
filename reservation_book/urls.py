from . import views
from django.urls import path

urlpatterns = [
    path('', views.ReservationDisplay.as_view(), name='home'),
    path('reservation_book/',
         views.reservation_detail, name='reservation_id'),
]
