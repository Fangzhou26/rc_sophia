import asyncio
import json
import random
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest

from app.database import Database
from app.models import NotificationCreate
from app.worker import Worker, retry_delay


class Clock:
    def __init__(self):
        self.now = 2_000_000_000

    def __call__(self):
        return self.now


@pytest.fixture
def job(settings, payload):
    database = Database(settings)
    database.initialize()
    payload["headers"] = {"Authorization": "Bearer private"}
    task_id = database.create(NotificationCreate(**payload))["id"]
    return database, task_id, Clock()


def row(database, task_id):
    with database.connect() as connection:
        return dict(connection.execute("SELECT * FROM notifications WHERE id = ?",
                                       (task_id,)).fetchone())


async def execute(database, clock, handler, count=1, **kwargs):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        worker = Worker(database, client, clock=clock, rng=random.Random(42), **kwargs)
        for _ in range(count):
            await worker.run_once()


@pytest.mark.parametrize("code", [200, 201, 202, 204, 299])
def test_success(job, code):
    database, task_id, clock = job
    requests = []

    def handler(request):
        requests.append(request)
        # A separate writer can acquire the lock while the HTTP request is active.
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        return httpx.Response(code)

    asyncio.run(execute(database, clock, handler, count=2))
    assert len(requests) == 1
    assert requests[0].headers["Idempotency-Key"] == task_id
    assert requests[0].headers["Content-Type"] == "application/json"
    assert json.loads(requests[0].content) == {"user_id": 123}
    result = row(database, task_id)
    assert result["status"] == "SUCCESS" and result["attempt_count"] == 1
    assert all(result[key] is None for key in ("lease_until", "lease_token", "next_retry_at", "last_error"))


@pytest.mark.parametrize("failure", [408, 429, 500, 503, "timeout", "connect", "protocol"])
def test_retry_then_success_and_stable_request(job, failure):
    database, task_id, clock = job
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("private URL and secret", request=request)
            if failure == "connect":
                raise httpx.ConnectError("private", request=request)
            if failure == "protocol":
                raise httpx.RemoteProtocolError("private", request=request)
            return httpx.Response(failure, headers={"Set-Cookie": "supplier=secret"})
        return httpx.Response(200)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = Worker(database, client, clock, random.Random(1))
            await worker.run_once()
            pending = row(database, task_id)
            assert pending["status"] == "PENDING" and pending["attempt_count"] == 1
            assert 8 <= pending["next_retry_at"] - clock.now <= 12
            assert pending["lease_token"] is None and pending["lease_until"] is None
            assert "private" not in pending["last_error"]
            assert not await worker.run_once()
            clock.now = pending["next_retry_at"]
            await worker.run_once()

    asyncio.run(scenario())
    assert row(database, task_id)["status"] == "SUCCESS"
    assert row(database, task_id)["attempt_count"] == 2
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert requests[0].headers == requests[1].headers
    assert requests[0].url == requests[1].url
    assert requests[0].method == requests[1].method == "POST"


@pytest.mark.parametrize("code", [301, 302, 307, 400, 401, 403, 404, 409])
def test_permanent_failure_and_no_redirect(job, code):
    database, task_id, clock = job
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(code, headers={"Location": "https://evil.test/private"})

    asyncio.run(execute(database, clock, handler, count=2))
    result = row(database, task_id)
    assert len(seen) == 1
    assert result["status"] == "FAILED" and result["attempt_count"] == 1
    assert result["last_error"] == f"HTTP {code}"
    assert result["next_retry_at"] is None and result["lease_token"] is None


def test_exhausted_retries(job):
    database, task_id, clock = job
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(503)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = Worker(database, client, clock, random.Random(5))
            for attempt in range(1, 6):
                before = clock.now
                assert await worker.run_once()
                result = row(database, task_id)
                assert result["attempt_count"] == attempt
                if attempt < 5:
                    base = 10 * 2 ** (attempt - 1)
                    assert result["status"] == "PENDING"
                    assert base * .8 <= result["next_retry_at"] - before <= base * 1.2
                    clock.now = result["next_retry_at"]
            assert not await worker.run_once()

    asyncio.run(scenario())
    result = row(database, task_id)
    assert len(seen) == 5 and result["status"] == "FAILED"
    assert result["next_retry_at"] is None and result["last_error"] == "HTTP 503"


def test_lease_recovery(job):
    database, task_id, clock = job
    first = database.claim(clock)
    assert first["attempt_count"] == 1
    clock.now += 59
    assert database.claim(clock) is None
    clock.now += 1
    asyncio.run(execute(database, clock, lambda request: httpx.Response(200)))
    assert row(database, task_id)["status"] == "SUCCESS"
    assert row(database, task_id)["attempt_count"] == 2
    assert not database.finish(first, "FAILED", clock=clock)


def test_old_token_cannot_replace_new_owner(job):
    database, task_id, clock = job
    old = database.claim(clock)
    clock.now += 60
    new = database.claim(clock)
    assert old["lease_token"] != new["lease_token"]
    assert not database.finish(old, "FAILED", clock=clock)
    assert row(database, task_id)["lease_token"] == new["lease_token"]
    assert database.finish(new, "SUCCESS", clock=clock)
    assert not database.finish(old, "PENDING", delay=10, clock=clock)
    assert row(database, task_id)["status"] == "SUCCESS"


def test_expired_result_without_reclaim_is_rejected(job):
    database, task_id, clock = job
    task = database.claim(clock)
    clock.now += 60
    assert not database.finish(task, "SUCCESS", clock=clock)
    assert row(database, task_id)["status"] == "PROCESSING"


def test_five_crashes_never_send_sixth(job):
    database, task_id, clock = job
    for attempt in range(1, 6):
        assert database.claim(clock)["attempt_count"] == attempt
        clock.now += 60

    def must_not_send(request):
        pytest.fail("Exhausted lease must not send a sixth request")

    asyncio.run(execute(database, clock, must_not_send))
    result = row(database, task_id)
    assert result["status"] == "FAILED" and result["attempt_count"] == 5
    assert result["last_error"] == "lease expired after max attempts"
    assert result["lease_token"] is None and result["next_retry_at"] is None


def test_concurrent_claims_use_independent_connections(job):
    database, task_id, clock = job
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=5)
        return Database(database.settings).claim(clock)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))
    assert sum(task is not None for task in results) == 1
    assert row(database, task_id)["attempt_count"] == 1


def test_success_then_db_write_failure_may_duplicate(job, monkeypatch):
    database, task_id, clock = job
    finish = database.finish
    seen = []

    def fail(*args):
        raise sqlite3.OperationalError("unavailable")

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(database, "finish", fail)
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(execute(database, clock, handler))
    assert row(database, task_id)["status"] == "PROCESSING"
    monkeypatch.setattr(database, "finish", finish)
    clock.now += 60
    asyncio.run(execute(database, clock, handler))
    assert row(database, task_id)["status"] == "SUCCESS"
    assert row(database, task_id)["attempt_count"] == 2
    assert len(seen) == 2
    assert seen[0].headers["Idempotency-Key"] == seen[1].headers["Idempotency-Key"]


def test_total_timeout(job):
    database, task_id, clock = job
    cancelled = []

    async def handler(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    asyncio.run(execute(database, clock, handler, request_timeout=.02))
    result = row(database, task_id)
    assert cancelled == [True]
    assert result["status"] == "PENDING" and result["last_error"] == "Request timeout"


def test_response_body_is_not_read(job):
    database, task_id, clock = job
    closed = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("Supplier body must not be consumed")
            yield b""

        async def aclose(self):
            closed.append(True)

    asyncio.run(execute(database, clock, lambda request: httpx.Response(202, stream=Body())))
    assert row(database, task_id)["status"] == "SUCCESS"
    assert closed == [True]


def test_allowlist_change_blocks_delivery(job):
    database, task_id, clock = job
    from app.config import Settings
    database = Database(Settings(database.settings.db_path))

    def must_not_send(request):
        pytest.fail("Removed supplier must not receive requests")

    asyncio.run(execute(database, clock, must_not_send))
    assert row(database, task_id)["status"] == "FAILED"
    assert row(database, task_id)["last_error"] == "Destination no longer allowed"


def test_jitter_limits_and_cap():
    class Rng:
        def __init__(self, value):
            self.value = value

        def uniform(self, low, high):
            assert (low, high) == (.8, 1.2)
            return self.value

    for n, base in enumerate([10, 20, 40, 80], 1):
        assert retry_delay(n, Rng(.8)) == round(base * .8)
        assert retry_delay(n, Rng(1.2)) == round(base * 1.2)
    assert retry_delay(10, Rng(1.2)) == 300


def test_real_process_exit_after_claim_recovers(job):
    database, task_id, clock = job
    script = """
import os
import sys
from pathlib import Path
from app.config import Settings
from app.database import Database
database = Database(Settings(Path(sys.argv[1])))
task = database.claim(lambda: int(sys.argv[2]))
os._exit(17 if task else 18)
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(database.settings.db_path), str(clock.now)],
        timeout=10, capture_output=True,
    )
    assert process.returncode == 17, process.stderr.decode()
    assert row(database, task_id)["status"] == "PROCESSING"
    assert database.claim(clock) is None
    clock.now += 60
    reopened = Database(database.settings)
    asyncio.run(execute(reopened, clock, lambda request: httpx.Response(200)))
    result = row(reopened, task_id)
    assert result["status"] == "SUCCESS" and result["attempt_count"] == 2


def test_escaped_json_remains_sendable_and_stable(settings):
    database = Database(settings)
    database.initialize()
    body = {"unicode": "你好", "escaped": "\ud800"}
    task_id = database.create(NotificationCreate(
        url="https://crm.example.com/webhook", body=body
    ))["id"]
    seen = []

    def handler(request):
        seen.append(request.content)
        return httpx.Response(200)

    asyncio.run(execute(database, Clock(), handler))
    assert row(database, task_id)["status"] == "SUCCESS"
    assert json.loads(seen[0]) == body


def test_worker_log_does_not_include_credentials(job, caplog):
    database, task_id, clock = job

    def handler(request):
        raise httpx.ConnectError("Bearer private; URL contains secret", request=request)

    with caplog.at_level("INFO", logger="app.worker"):
        asyncio.run(execute(database, clock, handler))
    records = [record.getMessage() for record in caplog.records if record.name == "app.worker"]
    assert len(records) == 1 and task_id in records[0]
    assert "private" not in records[0] and "secret" not in records[0]
    assert row(database, task_id)["last_error"] == "Network error"


def test_legacy_oversized_url_fails_without_stopping_worker(job):
    database, task_id, clock = job
    with database.connect() as connection:
        with connection:
            connection.execute("UPDATE notifications SET url = ? WHERE id = ?",
                               ("https://crm.example.com/" + "x" * 66000, task_id))
    next_id = database.create(NotificationCreate(
        url="https://crm.example.com/valid", body={}
    ))["id"]
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200)

    asyncio.run(execute(database, clock, handler, count=3))
    invalid = row(database, task_id)
    assert invalid["status"] == "FAILED" and invalid["attempt_count"] == 1
    assert invalid["last_error"] == "Invalid destination URL"
    assert invalid["lease_token"] is None and invalid["next_retry_at"] is None
    assert row(database, next_id)["status"] == "SUCCESS"
    assert seen == ["https://crm.example.com/valid"]
