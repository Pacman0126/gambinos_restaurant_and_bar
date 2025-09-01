from . import views
from django.urls import path
from .views import home

urlpatterns = [
    path('', home, name='home'),
    # path('reservation_book/',
    # views.reservation_detail, name='reservation_detail'),
]
