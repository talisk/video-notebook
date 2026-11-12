"""Gemini Notebook OAuth（授权码）。客户端密钥只从环境变量读取。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from jose import JWTError, jwt

from app.core.config import get_settings
from app.core.errors import AppError, BadRequestError
from app.notebooklm.http import AUTH_URL, NOTEBOOK_SCOPE, TOKEN_URL, async_client


class NotebookLmOAuthError(AppError):
    status_code = 400
    code = "notebooklm_oauth_error"


def oauth_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.notebooklm_oauth_client_id.strip()
        and settings.notebooklm_oauth_client_secret.strip()
    )


def redirect_uri_for(request_base: str) -> str:
    """授权请求与令牌交换必须使用同一个重定向 URI。"""
    settings = get_settings()
    explicit = settings.notebooklm_oauth_redirect_uri.strip()
    if explicit:
        return explicit
    public = settings.public_base_url.strip().rstrip("/")
    if public:
        return f"{public}/api/notebooklm/oauth/callback"
    base = request_base.strip().rstrip("/")
    return f"{base}/api/notebooklm/oauth/callback"


def encode_oauth_state(*, user_id: int, origin: str, next_path: str, redirect_uri: str) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "type": "notebooklm_oauth",
        "origin": origin,
        "next": next_path,
        "redirect_uri": redirect_uri,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_oauth_state(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise NotebookLmOAuthError("授权状态已失效，请重新连接 Google") from exc
    if payload.get("type") != "notebooklm_oauth":
        raise NotebookLmOAuthError("授权状态无效，请重新连接 Google")
    return payload


def build_authorize_url(*, state: str, redirect_uri: str) -> str:
    settings = get_settings()
    query = urlencode(
        {
            "client_id": settings.notebooklm_oauth_client_id.strip(),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": NOTEBOOK_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


def safe_next_path(path: str | None) -> str:
    default = "/settings?section=notebooklm"
    if not path:
        return default
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return default
    if len(path) > 500:
        return default
    return path


def origin_allowed(origin: str, *, request_host: str | None = None) -> bool:
    from urllib.parse import urlsplit

    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return False
    if parts.username or parts.password:
        return False
    host = (parts.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    if request_host and host == request_host.lower().split(":")[0]:
        return True
    settings = get_settings()
    for base in (settings.web_origin, settings.public_base_url):
        allowed_host = (urlsplit(base).hostname or "").lower()
        if allowed_host and allowed_host == host:
            return True
    return False


async def exchange_auth_code(*, code: str, redirect_uri: str) -> dict[str, Any]:
    return await _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
    )


async def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    return await _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    )


async def _token_request(form: dict[str, str]) -> dict[str, Any]:
    if not oauth_configured():
        raise BadRequestError("尚未配置 Gemini Notebook OAuth 客户端")
    settings = get_settings()
    payload = {
        **form,
        "client_id": settings.notebooklm_oauth_client_id.strip(),
        "client_secret": settings.notebooklm_oauth_client_secret.strip(),
    }
    async with async_client() as client:
        response = await client.post(TOKEN_URL, data=payload)
    data = _json_or_empty(response)
    if response.status_code >= 400:
        reason = str(data.get("error_description") or data.get("error") or response.reason_phrase)
        if data.get("error") == "invalid_grant":
            raise NotebookLmOAuthError("Google 授权已失效，请在设置里重新连接")
        raise NotebookLmOAuthError(f"Google 授权失败：{_clip(reason)}")
    if not data.get("access_token"):
        raise NotebookLmOAuthError("Google 没有返回访问令牌")
    return data


def _json_or_empty(response) -> dict[str, Any]:
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _clip(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
