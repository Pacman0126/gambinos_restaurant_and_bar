from django.shortcuts import render, get_object_or_404, redirect
# from django.views import generic
from .models import ReservationBook
from .forms import ReservationsForm


# from django.http import HttpResponse


# Create your views here.
# def reservations(request):
#    return HttpResponse("Welcome Gambinos reservation book")

# class ReservationDisplay(generic.ListView):
# model = ReservationBook
# queryset = ReservationBook.objects.all()
# template_name = "reservation_book/index.html"


def home(request):
    """
    Display an individual :model:`reservation_book.ReservationBook`.

    **Context**

    ``post``
        An instance of :model:`reservation_book.ReservationBook`.

    **Template:**

    :template:`reservation_book/index.html`
    """

    reservations = ReservationBook.objects.all()
    # reservation = get_object_or_404(queryset, reservation_id=reservation_id)

    return render(
        request,
        "reservation_book/templates/index.html",
        {"reservations": reservations}
    )
