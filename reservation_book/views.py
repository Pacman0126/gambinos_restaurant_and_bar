from django.shortcuts import render
from django.views import generic
from .models import ReservationBook
# from django.http import HttpResponse


# Create your views here.
# def reservations(request):
#    return HttpResponse("Welcome Gambinos reservation book")

class ReservationDisplay(generic.ListView):
    # model = ReservationBook
    queryset = ReservationBook.objects.all()
    template_name = "reservation_book/index.html"
