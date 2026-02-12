import pytest
from django.urls import reverse
from django.contrib.auth import get_user_model

pytestmark = pytest.mark.django_db

User = get_user_model()


def make_user(**kwargs):
    defaults = dict(username="u", email="u@example.com",
                    password="pass12345", is_active=True)
    defaults.update(kwargs)
    u = User.objects.create_user(
        username=defaults["username"],
        email=defaults["email"],
        password=defaults["password"],
    )
    u.is_staff = defaults.get("is_staff", False)
    u.is_superuser = defaults.get("is_superuser", False)
    u.is_active = defaults.get("is_active", True)
    u.save()
    return u


@pytest.mark.parametrize("name,args", [
    ("staff_dashboard", []),
    ("staff_reservations", []),
    ("user_reservations_overview", []),
    ("create_phone_reservation", []),
])
def test_staff_pages_block_nonstaff(client, name, args):
    user = make_user(username="cust", email="cust@example.com", is_staff=False)
    client.login(username="cust", password="pass12345")
    url = reverse(name, args=args)
    resp = client.get(url)
    assert resp.status_code in (302, 403)


@pytest.mark.parametrize("name,args", [
    ("staff_management", []),
    ("add_staff", []),
])
def test_superuser_pages_block_staff(client, name, args):
    staff = make_user(username="staff",
                      email="staff@example.com", is_staff=True)
    client.login(username="staff", password="pass12345")
    url = reverse(name, args=args)
    resp = client.get(url)
    assert resp.status_code in (302, 403)


def test_staff_can_access_staff_pages(client):
    staff = make_user(username="staff2",
                      email="staff2@example.com", is_staff=True)
    client.login(username="staff2", password="pass12345")
    resp = client.get(reverse("staff_dashboard"))
    assert resp.status_code == 200
