from django import template
# adjust import path if needed
from reservation_book.constants import SLOT_LABELS

register = template.Library()


@register.filter
def get_item(d, k):
    try:
        return d.get(k)
    except Exception:
        return None
