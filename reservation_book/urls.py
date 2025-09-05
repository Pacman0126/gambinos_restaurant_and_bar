from . import views
from django.urls import path
from .views import home, reservations, importExcel, make_reservation

urlpatterns = [
    path('', home, name='home'),
    path('reservation_book/', make_reservation, name='make_reservation'),
    path('reservation_book/', reservations, name='reservations'),
    path("reservation_success/", views.reservation_success,
         name="reservation_success"),
    path('import/', importExcel, name='push_excel'),

    # views.reservation_detail, name='reservation_detail'),
]
