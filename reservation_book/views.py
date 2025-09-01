from django.shortcuts import render, get_object_or_404
from django.views import generic
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


def reservation_detail(request, reservation_id):
    """
    Display an individual :model:`reservation_book.ReservationBook`.

    **Context**

    ``post``
        An instance of :model:`reservation_book.ReservationBook`.

    **Template:**

    :template:`reservation_book/index.html`
    """

    queryset = ReservationBook.objects.all()
    post = get_object_or_404(queryset, reservation_id=reservation_id)

    return render(
        request,
        "reservation_book/index.html",
        {"post": post},
    )
