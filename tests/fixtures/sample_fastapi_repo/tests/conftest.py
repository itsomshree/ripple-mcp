import pytest
from app.main import app
from app.services.user_service import UserService
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(app)


def make_user(name: str = "ada") -> dict:
    return UserService.create({"name": name})
