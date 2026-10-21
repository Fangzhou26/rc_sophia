import ipaddress
import re
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, JsonValue, field_validator
from typing import Literal


RESERVED_HEADERS = {
    "idempotency-key", "host", "content-length", "connection",
    "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "transfer-encoding", "upgrade",
}
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
HOSTNAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")


class NotificationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    url: str
    method: Literal["POST"] = "POST"
    headers: dict[str, str] = {}
    body: JsonValue

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        try:
            httpx.URL(value)
        except httpx.InvalidURL:
            raise ValueError("Invalid or oversized destination URL") from None
        if any(ord(char) <= 32 or ord(char) >= 127 for char in value) or "\\" in value:
            raise ValueError("URL must be an ASCII HTTPS URL without whitespace")
        try:
            parts = urlsplit(value)
            host = parts.hostname
            port = parts.port
        except ValueError:
            raise ValueError("Invalid URL") from None
        if (parts.scheme != "https" or not host or parts.username is not None
                or parts.password is not None or "#" in value or port not in (None, 443)
                or parts.netloc.endswith(":")):
            raise ValueError("HTTPS on port 443 without user information or fragment required")
        if len(host) > 253 or not HOSTNAME.fullmatch(host):
            raise ValueError("Invalid hostname")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return value
        raise ValueError("IP literal destinations are not allowed")

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, headers):
        seen = set()
        for name, value in headers.items():
            key = name.lower()
            if not HEADER_NAME.fullmatch(name) or key in RESERVED_HEADERS or key in seen:
                raise ValueError("Invalid, reserved or duplicate header name")
            if any(ord(char) < 32 or ord(char) >= 127 for char in value):
                raise ValueError("Header values must contain printable ASCII characters")
            if value != value.strip():
                raise ValueError("Header values must not start or end with whitespace")
            seen.add(key)
        return headers


class NotificationAccepted(BaseModel):
    id: str
    status: Literal["PENDING"]


class NotificationStatus(BaseModel):
    id: str
    status: Literal["PENDING", "PROCESSING", "SUCCESS", "FAILED"]
    attempt_count: int
    last_error: str | None
    next_retry_at: int | None
    created_at: int
    updated_at: int
