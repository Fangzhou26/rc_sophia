import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "notifications.sqlite3", frozenset({"crm.example.com"}))


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def payload():
    return {"url": "https://crm.example.com/webhook", "body": {"user_id": 123}}
