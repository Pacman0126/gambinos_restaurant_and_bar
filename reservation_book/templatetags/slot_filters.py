from django import template
# adjust import path if needed
from reservation_book.views import SLOT_LABELS

register = template.Library()


@register.filter
def slot_label(value):
    return SLOT_LABELS.get(value, value)
