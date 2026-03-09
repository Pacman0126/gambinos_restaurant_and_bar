from django.apps import AppConfig


class ReservationBookConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "reservation_book"

    def ready(self):
        # Import signals so the receiver is registered
        import reservation_book.signals  # noqa
