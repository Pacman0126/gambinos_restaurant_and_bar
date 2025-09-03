from . import views
from django.urls import path
from .views import home, reservations

urlpatterns = [
    path('', home, name='home'),
    path('reservation_book/', reservations, name='reservations'),
    # views.reservation_detail, name='reservation_detail'),
]
