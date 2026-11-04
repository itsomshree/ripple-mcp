from app.services.user_service import UserService
from tests.conftest import make_user


def test_get_user_service():
    assert UserService.get(1) is None


def test_get_user_endpoint(client):
    response = client.get("/users/1")
    assert response.status_code in (200, 404)


def test_create_user_via_helper():
    assert make_user("grace")["name"] == "grace"
