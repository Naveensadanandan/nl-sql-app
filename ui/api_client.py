"""Thin HTTP client for the NL-SQL API. The UI never talks to a database directly.

The login token is kept in Streamlit's server-side session state (never in the browser URL or
cookies) and sent as `Authorization: Bearer <token>` on every call.
"""

import os
from typing import Any

import httpx

API_URL = os.environ.get("NLSQL_API_URL", "http://localhost:8000").rstrip("/")
# A question runs several LLM calls (each up to NLSQL_LLM_TIMEOUT_SECONDS, with retries).
ASK_TIMEOUT = float(os.environ.get("NLSQL_UI_ASK_TIMEOUT", "300"))


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _message(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except ValueError:
        return resp.text[:300] or resp.reason_phrase
    if isinstance(detail, list):  # FastAPI validation errors
        return "; ".join(
            f"{'.'.join(map(str, d.get('loc', [])[1:]))}: {d.get('msg')}" for d in detail
        )
    return str(detail or resp.reason_phrase)


def call(
    method: str,
    path: str,
    token: str | None = None,
    timeout: float = 30.0,
    **kwargs: Any,
) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = httpx.request(method, f"{API_URL}{path}", headers=headers, timeout=timeout, **kwargs)
    except httpx.HTTPError as e:
        raise ApiError(0, f"cannot reach the API at {API_URL} ({type(e).__name__})") from e
    if resp.status_code >= 400:
        raise ApiError(resp.status_code, _message(resp))
    return resp.json() if resp.content else None
