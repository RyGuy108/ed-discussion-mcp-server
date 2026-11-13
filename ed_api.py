"""Restricted Ed Discussion API transport and response validation."""
from __future__ import annotations

import asyncio
import json
import os
import re
from urllib.parse import urlsplit

import httpx

HOSTS = {
    "us": "https://us.edstem.org",
    "au": "https://au.edstem.org",
    "eu": "https://eu.edstem.org",
}
READ_ROUTES = re.compile(
    r"/api/(?:user|threads/[1-9][0-9]*|courses/[1-9][0-9]*/threads(?:/[1-9][0-9]*)?)"
)
MAX_BYTES = 8 * 1024 * 1024


class EdError(Exception):
    """A safe, actionable message to surface to the MCP client."""


class EdUnavailable(EdError):
    """An upstream or transport failure that may be temporary."""


class EdRateLimited(EdUnavailable):
    """Ed asked us to stop requesting data."""


class EdAccessDenied(EdError):
    """The token cannot access an Ed object."""


class EdNotFound(EdError):
    """The requested Ed object is absent or not visible."""


def rows(obj: dict, key: str, *, required: bool = False) -> list[dict]:
    value = obj.get(key, [] if not required else None)
    if value is None and not required:
        return []
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise EdError(f"Unexpected Ed API response for '{key}'. Check API compatibility.")
    return value


def ed_id(value: object) -> bool:
    return type(value) is int and value > 0


class EdAPI:
    def __init__(self, token: str, region: str = "us", transport=None):
        region = region.strip().lower()
        if region not in HOSTS:
            raise EdError("ED_REGION must be us, au, or eu.")
        self.region = region
        self._token = token.strip()
        self._transport = transport

    @classmethod
    def from_env(cls) -> EdAPI:
        return cls(os.getenv("ED_API_TOKEN", ""), os.getenv("ED_REGION", "us"))

    async def _get(self, path: str, **params: int | str) -> dict:
        if not READ_ROUTES.fullmatch(path):
            raise EdError("Blocked request to an unapproved Ed API route.")
        if not self._token:
            raise EdError("Set ED_API_TOKEN in the environment that launches this server.")
        if not re.fullmatch(r"[!-~]+", self._token):
            raise EdError("ED_API_TOKEN contains invalid whitespace or characters.")
        try:
            async with asyncio.timeout(20), httpx.AsyncClient(
                base_url=HOSTS[self.region], transport=self._transport,
                follow_redirects=False, trust_env=False, timeout=15,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json",
                         "User-Agent": "ed-discussion-read-mcp/1.0"},
            ) as client:
                async with client.stream("GET", path, params=params) as response:
                    status = response.status_code
                    if status in (401, 403):
                        raise EdAccessDenied(f"Ed denied access ({status}). Check token, region, and course access.")
                    if status == 404:
                        raise EdNotFound("Ed could not find that course or thread, or it is not visible to this account.")
                    if status == 429:
                        raise EdRateLimited("Ed rate-limited the request. Wait and retry with a smaller scan.")
                    if 300 <= status < 400:
                        raise EdError("Ed redirected the request. Check ED_REGION and token settings.")
                    if 500 <= status < 600:
                        raise EdUnavailable(f"Ed returned HTTP {status}.")
                    if status != 200:
                        raise EdError(f"Ed returned HTTP {status}.")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise EdError("Ed response exceeds the 8 MiB limit. Try a smaller page.")
        except (httpx.HTTPError, TimeoutError):
            raise EdUnavailable("Could not reach the Ed API. Check your connection and region.") from None
        try:
            payload = json.loads(data)
        except (ValueError, RecursionError):
            raise EdError("Ed returned invalid JSON. Check the API region and compatibility.") from None
        if not isinstance(payload, dict):
            raise EdError("Expected a JSON object from Ed.")
        return payload

    async def account(self) -> dict:
        return await self._get("/api/user")

    async def page(self, course_id: int, limit: int, offset: int) -> dict:
        return await self._get(f"/api/courses/{course_id}/threads", limit=limit,
                               offset=offset, sort="new")

    async def detail(self, *, thread_id: int | None = None, course_id: int | None = None,
                     number: int | None = None) -> dict:
        path = f"/api/threads/{thread_id}" if thread_id is not None else (
            f"/api/courses/{course_id}/threads/{number}")
        payload = await self._get(path)
        thread = payload.get("thread")
        if not isinstance(thread, dict) or not all(ed_id(thread.get(k)) for k in ("id", "course_id")):
            raise EdError("Ed returned a thread without valid identifiers.")
        if ((thread_id is not None and thread["id"] != thread_id)
                or (course_id is not None and thread["course_id"] != course_id)
                or (number is not None and thread.get("number") != number)):
            raise EdError("Ed returned a thread that does not match the requested identifiers.")
        return payload

    def link(self, course_id: int, thread_id: int | None = None) -> str:
        base = f"https://edstem.org/{self.region}/courses/{course_id}/discussion"
        return base if thread_id is None else f"{base}/{thread_id}"

    def parse_link(self, link: str) -> tuple[int, int]:
        try:
            parsed = urlsplit(link)
        except ValueError:
            raise EdError("Use an Ed Discussion thread URL in the configured region.") from None
        region = re.escape(self.region)
        patterns = {
            "edstem.org": rf"/{region}/courses/([1-9][0-9]{{0,15}})/discussion/([1-9][0-9]{{0,15}})/?",
            f"{self.region}.edstem.org": rf"/(?:{region}/)?courses/([1-9][0-9]{{0,15}})/discussion/([1-9][0-9]{{0,15}})/?",
        }
        if parsed.scheme != "https" or parsed.netloc not in patterns:
            raise EdError("Use an HTTPS Ed Discussion thread URL in the configured region.")
        match = re.fullmatch(patterns[parsed.netloc], parsed.path)
        if not match:
            raise EdError("Use an Ed Discussion thread URL, not a course homepage.")
        return int(match[1]), int(match[2])
