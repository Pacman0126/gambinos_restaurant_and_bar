from django.contrib import admin
from django_summernote.admin import SummernoteModelAdmin
from .models import ContactMobile
from .models import ContactPhone
from .models import ReservationBook
from .models import TableReservation
from .models import OnlineRegisteredCustomer
from .models import ReservedTables1718
from .models import ReservedTables1819
from .models import ReservedTables1920
from .models import ReservedTables2021
from .models import ReservedTables2122
from .models import BridgeEntity
from .models import Creditos1


# Register your models here.
@admin.register(ReservationBook)
class PostAdmin(SummernoteModelAdmin):

    list_display = ('reservation_date', 'first_name')
    search_fields = ['reservation_date']
    # list_filter = ('status',)
    # prepopulated_fields = {'slug': ('title',)}
    summernote_fields = ('reservation_date',)


@admin.register(ContactMobile)
class ContactAdminMobile(admin.ModelAdmin):
    pass


@admin.register(ContactPhone)
class ContactAdminPhone(admin.ModelAdmin):
    pass


# admin.register(ReservationBook)
admin.register(TableReservation)
admin.register(OnlineRegisteredCustomer)
admin.register(ReservedTables1718)
admin.register(ReservedTables1819)
admin.register(ReservedTables1920)
admin.register(ReservedTables2021)
admin.register(ReservedTables2122)
admin.register(BridgeEntity)
admin.register(Creditos1)
