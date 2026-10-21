import json
import sqlite3
from contextlib import contextmanager
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.main import create_app


def test_accept_committed_task(client, settings, payload):
    payload["headers"] = {"Authorization": "Bearer secret"}
    response = client.post("/notifications", json=payload)
    assert response.status_code == 202
    accepted = response.json()
    UUID(accepted["id"])
    assert accepted["status"] == "PENDING"
    # Independent connection observes the committed record before querying API.
    with Database(settings).connect() as connection:
        row = connection.execute("SELECT * FROM notifications WHERE id = ?",
                                 (accepted["id"],)).fetchone()
    assert row["status"] == "PENDING"
    assert row["attempt_count"] == 0
    assert row["method"] == "POST"
    assert row["next_retry_at"] == row["created_at"] == row["updated_at"]
    assert row["lease_until"] is None and row["lease_token"] is None
    assert json.loads(row["body"]) == payload["body"]
    assert json.loads(row["headers"]) == payload["headers"]
    result = client.get(f'/notifications/{accepted["id"]}')
    assert result.status_code == 200
    assert set(result.json()) == {"id", "status", "attempt_count", "last_error",
                                  "next_retry_at", "created_at", "updated_at"}
    assert "secret" not in result.text


@pytest.mark.parametrize("body", [None, True, False, 123, 1.5, "hello", [], {}, [1, None]])
def test_json_values(client, settings, payload, body):
    payload["body"] = body
    response = client.post("/notifications", json=payload)
    assert response.status_code == 202
    with Database(settings).connect() as connection:
        stored = connection.execute("SELECT body FROM notifications WHERE id = ?",
                                    (response.json()["id"],)).fetchone()[0]
    assert json.loads(stored) == body


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", '{"nested":NaN}'])
def test_nonstandard_json_rejected(client, value):
    response = client.post("/notifications", content=(
        '{"url":"https://crm.example.com/webhook","body":' + value + '}'
    ), headers={"Content-Type": "application/json"})
    assert response.status_code == 422


def test_missing_body_rejected(client):
    assert client.post("/notifications", json={"url": "https://crm.example.com"}).status_code == 422


@pytest.mark.parametrize("url", [
    "http://crm.example.com", "https://evil.example", "https://crm.example.com.evil.test",
    "https://sub.crm.example.com", "https://crm.example.com:444",
    "https://user:secret@crm.example.com", "https://crm.example.com/#fragment",
    "https://127.0.0.1", "https://[::1]", "https://crm.example.com\n",
    "https://crm.example.com\\@evil.test", "https://crm.example.com:",
])
def test_invalid_destinations(client, payload, url):
    payload["url"] = url
    assert client.post("/notifications", json=payload).status_code == 422


def test_normalized_hostname_and_port(client, payload):
    payload["url"] = "https://CRM.EXAMPLE.COM:443/webhook"
    assert client.post("/notifications", json=payload).status_code == 202


def test_empty_allowlist(settings, payload):
    with TestClient(create_app(Settings(settings.db_path))) as client:
        assert client.post("/notifications", json=payload).status_code == 422


@pytest.mark.parametrize("headers", [
    {"IdEmPoTeNcY-KeY": "custom"}, {"Host": "evil.test"}, {"Content-Length": "1"},
    {"Transfer-Encoding": "chunked"}, {"Connection": "close"},
    {"Authorization": "secret\r\nInjected: yes"}, {"Bad Header": "value"},
    {"X-Test": "a", "x-test": "b"}, {"X-Test": 123},
])
def test_invalid_headers(client, payload, headers):
    payload["headers"] = headers
    assert client.post("/notifications", json=payload).status_code == 422


@pytest.mark.parametrize("method", ["GET", "PUT", "post", None])
def test_invalid_method(client, payload, method):
    payload["method"] = method
    assert client.post("/notifications", json=payload).status_code == 422


def test_validation_does_not_echo_secrets(client, payload):
    payload["headers"] = {"Authorization": "secret\nvalue"}
    payload["body"] = {"password": "private-body"}
    response = client.post("/notifications", json=payload)
    assert response.status_code == 422
    assert "secret" not in response.text and "private-body" not in response.text


def test_missing_id(client):
    assert client.get("/notifications/missing").status_code == 404
    assert client.get("/notifications/' OR 1=1 --").status_code == 404


def test_insert_failure(client, payload, monkeypatch):
    def fail(*args):
        raise sqlite3.OperationalError("sensitive database path")
    monkeypatch.setattr(client.app.state.database, "create", fail)
    response = client.post("/notifications", json=payload)
    assert response.status_code == 503
    assert "sensitive" not in response.text


def test_commit_failure_rolls_back(client, settings, payload, monkeypatch):
    class FailingCommit(sqlite3.Connection):
        def __exit__(self, exc_type, exc, traceback):
            self.rollback()
            raise sqlite3.OperationalError("commit failed")

    @contextmanager
    def connect():
        connection = sqlite3.connect(str(settings.db_path), factory=FailingCommit)
        try:
            yield connection
        finally:
            connection.close()

    monkeypatch.setattr(client.app.state.database, "connect", connect)
    assert client.post("/notifications", json=payload).status_code == 503
    with Database(settings).connect() as connection:
        assert connection.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0


def test_persistence_across_app_restart(settings, payload):
    with TestClient(create_app(settings)) as first:
        task_id = first.post("/notifications", json=payload).json()["id"]
    with TestClient(create_app(settings)) as second:
        response = second.get(f"/notifications/{task_id}")
        assert response.status_code == 200
        assert response.json()["status"] == "PENDING"


def test_sqlite_configuration(client, settings):
    with Database(settings).connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == settings.busy_timeout_ms


@pytest.mark.parametrize("body", ["\ud800", {"\udfff": "value"}, ["\ud800"]])
def test_escaped_surrogate_json_is_persistable(client, settings, payload, body):
    # Escaped surrogate code units can reach Python via the JSON decoder.
    payload["body"] = body
    response = client.post("/notifications", content=json.dumps(payload),
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 202
    with Database(settings).connect() as connection:
        stored = connection.execute("SELECT body FROM notifications WHERE id = ?",
                                    (response.json()["id"],)).fetchone()[0]
    assert stored.encode("utf-8")
    assert json.loads(stored) == body


def test_validation_does_not_echo_user_controlled_field_names(client, payload):
    payload["secret-field-value"] = "ignored"
    response = client.post("/notifications", json=payload)
    assert response.status_code == 422
    assert "secret-field-value" not in response.text


def test_validation_does_not_echo_header_keys(client, payload):
    payload["headers"] = {"secret-header-key": 123}
    response = client.post("/notifications", json=payload)
    assert response.status_code == 422
    assert "secret-header-key" not in response.text


@pytest.mark.parametrize("value", [" Bearer token", "Bearer token ", " "])
def test_header_values_with_edge_spaces_rejected(client, payload, value):
    payload["headers"] = {"Authorization": value}
    assert client.post("/notifications", json=payload).status_code == 422


def test_oversized_url_rejected_before_persistence(client, settings, payload):
    payload["url"] = "https://crm.example.com/" + "x" * 66000
    response = client.post("/notifications", json=payload)
    assert response.status_code == 422
    with Database(settings).connect() as connection:
        assert connection.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0
