from functools import wraps
from django.core.exceptions import PermissionDenied
from django.contrib.auth.views import redirect_to_login


def staff_or_superuser_required(view_func):
    """
    Allows access only to staff or superusers.
    Redirects anonymous users to login.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        user = request.user

        if not user.is_authenticated:
            return redirect_to_login(request.get_full_path())

        if not (user.is_staff or user.is_superuser):
            raise PermissionDenied("Staff access required.")

        return view_func(request, *args, **kwargs)

    return _wrapped_view
