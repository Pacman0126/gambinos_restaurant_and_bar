from django.contrib import admin
from .models import ContactMobile
from .models import ContactPhone
from .models import ReservationBook
from .models import TableReservation

# Register your models here.


@admin.register(ContactMobile)
class ContactAdminMobile(admin.ModelAdmin):
    pass


@admin.register(ContactPhone)
class ContactAdminPhone(admin.ModelAdmin):
    pass


admin.register(ReservationBook)
admin.register(TableReservation)
