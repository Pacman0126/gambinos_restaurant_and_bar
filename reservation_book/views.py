from django.shortcuts import render
from django.http import HttpResponse


# Create your views here.
def reservation_book(request):
    return HttpResponse("Hello, booking agent")
