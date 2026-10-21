"""Single, serial delivery worker. Start with python -m app.worker."""

import asyncio
import json
import logging
import random
import sqlite3
import time
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.database import Database


logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 10
LEASE_SECONDS = 60
POLL_SECONDS = 1


def retry_delay(attempt_count, rng):
    # Scheduling timestamps use integer Unix seconds, so round to the nearest second.
    return round(min(300, min(10 * 2 ** (attempt_count - 1), 300) * rng.uniform(0.8, 1.2)))


class Worker:
    def __init__(self, database, client, clock=time.time, rng=None, request_timeout=REQUEST_TIMEOUT):
        self.database = database
        self.client = client
        self.clock = clock
        self.rng = rng if rng is not None else random.Random()
        self.request_timeout = request_timeout

    async def deliver(self, task):
        if urlsplit(task["url"]).hostname not in self.database.settings.allowed_hosts:
            return False, "Destination no longer allowed", False
        headers = httpx.Headers(json.loads(task["headers"]))
        headers["Idempotency-Key"] = task["id"]
        if "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        # Explicit Cookie prevents the shared client's cookie jar from changing retries
        # or forwarding one supplier's cookies to another task.
        if "Cookie" not in headers:
            headers["Cookie"] = ""
        try:
            async with self.client.stream(
                task["method"], task["url"], headers=headers,
                content=task["body"].encode("utf-8"),
                timeout=self.request_timeout, follow_redirects=False,
            ) as response:
                code = response.status_code
                if 200 <= code < 300:
                    return True, None, False
                return False, f"HTTP {code}", code in (408, 429) or 500 <= code < 600
        except httpx.InvalidURL:
            return False, "Invalid destination URL", False
        except (httpx.TimeoutException, asyncio.TimeoutError):
            return False, "Request timeout", True
        except (httpx.NetworkError, httpx.RemoteProtocolError):
            return False, "Network error", True
        except httpx.RequestError:
            return False, "Request error", False

    async def run_once(self):
        task = await asyncio.to_thread(self.database.claim, self.clock, LEASE_SECONDS)
        if task is None:
            return False
        try:
            success, error, retryable = await asyncio.wait_for(
                self.deliver(task), timeout=self.request_timeout
            )
        except asyncio.TimeoutError:
            success, error, retryable = False, "Request timeout", True
        status = "SUCCESS" if success else "FAILED"
        delay = None
        if retryable and task["attempt_count"] < 5:
            status = "PENDING"
            delay = retry_delay(task["attempt_count"], self.rng)
        # A DB failure propagates separately: it says nothing about supplier receipt.
        applied = await asyncio.to_thread(
            self.database.finish, task, status, error, delay, self.clock
        )
        logger.info("task=%s attempt=%s result=%s applied=%s",
                    task["id"], task["attempt_count"], status, applied)
        return True

    async def run(self):
        while True:
            try:
                if await self.run_once():
                    continue
            except sqlite3.Error:
                logger.warning("Database operation failed; delivery outcome may be unknown")
            await asyncio.sleep(POLL_SECONDS)


async def main():
    database = Database(Settings.from_env())
    await asyncio.to_thread(database.initialize)
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, follow_redirects=False, trust_env=False
    ) as client:
        await Worker(database, client).run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # HTTPX INFO logs include full URLs, which can contain sensitive query values.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
