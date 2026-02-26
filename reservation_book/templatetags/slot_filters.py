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


@register.filter
def slot_label(slot_key: str) -> str:
    """
    Convert a slot key like '17_18' to '17:00–18:00'.
    Falls back to the original value if unknown/blank.
    """
    if not slot_key:
        return ""
    return SLOT_LABELS.get(slot_key, slot_key)
