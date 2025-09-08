from django.contrib import admin
from django_summernote.admin import SummernoteModelAdmin
# from .models import ContactMobile
# from .models import ContactPhone
from .models import ReservationBook
from .models import TableReservation
# from .models import OnlineRegisteredCustomer


# Register your models here.
@admin.register(ReservationBook)
class PostAdmin(SummernoteModelAdmin):

    list_display = ('reservation_date', 'first_name')
    search_fields = ['reservation_date']
    # list_filter = ('status',)
    # prepopulated_fields = {'slug': ('title',)}
    summernote_fields = ('reservation_date',)


# admin.register(ReservationBook)
admin.register(TableReservation)
