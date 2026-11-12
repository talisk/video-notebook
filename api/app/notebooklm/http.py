"""Gemini Notebook 与 Google OAuth 的 HTTP 出口。测试可换掉传输层。"""
from __future__ import annotations

from contextvars import ContextVar

import httpx

http_transport: ContextVar[httpx.AsyncBaseTransport | None] = ContextVar(
    "notebooklm_http_transport",
    default=None,
)

API_ROOT = "https://notebook.googleapis.com/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
NOTEBOOK_SCOPE = "https://www.googleapis.com/auth/notebook"


def async_client(**kwargs) -> httpx.AsyncClient:
    transport = http_transport.get()
    if transport is not None and "transport" not in kwargs:
        kwargs["transport"] = transport
    kwargs.setdefault("timeout", 30)
    return httpx.AsyncClient(**kwargs)
