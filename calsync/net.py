"""Small urllib wrapper with JSON handling and exponential backoff on 429/5xx."""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

RETRY_STATUSES = {429, 500, 502, 503, 504}


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str = ""):
        super().__init__(f"HTTP {status} {url}: {body[:300]}")
        self.status = status
        self.body = body


class AuthError(Exception):
    """Token missing, expired or revoked: the user must run `calsync auth` again."""


def request(method, url, *, headers=None, json_body=None, form=None, retries=5, timeout=60,
            sleep=time.sleep, opener=None):
    """Return parsed JSON (or {} for empty bodies). Retries 429/5xx and network errors."""
    data = None
    hdrs = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    open_ = opener or urllib.request.urlopen
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with open_(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace") if e.fp else ""
            if e.code in RETRY_STATUSES and attempt < retries:
                sleep(_delay(attempt, e.headers.get("Retry-After") if e.headers else None))
                attempt += 1
                continue
            raise HttpError(e.code, body, url) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                sleep(_delay(attempt, None))
                attempt += 1
                continue
            raise


def _delay(attempt: int, retry_after) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 120.0)
        except ValueError:
            pass
    return min(2 ** attempt, 60) + random.uniform(0, 0.5)
