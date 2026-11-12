"""Gemini Notebook API 客户端（notebook.googleapis.com/v1）。

公开预览只接受个人 Google 账号，来源可以是网页、YouTube，或不超过 1MB 的文本 / Markdown。
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.core.errors import AppError
from app.notebooklm.http import API_ROOT, async_client

_TERMINAL = {"ACTIVE", "FAILED"}


class NotebookLmApiError(AppError):
    code = "notebooklm_api_error"

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        self.status_code = status_code
        super().__init__(message)


class NotebookLmApi:
    def __init__(self, access_token: str, *, project_id: str = "") -> None:
        self._token = access_token
        self._project_id = project_id.strip()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> NotebookLmApi:
        self._client = async_client()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def create_notebook(self, title: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "notebooks",
            json={"display_name": title[:500]},
        )

    async def get_notebook(self, name: str) -> dict[str, Any]:
        return await self._request("GET", name)

    async def list_notebooks(self, *, page_size: int = 50, page_token: str | None = None) -> dict[str, Any]:
        params: dict[str, str | int] = {"pageSize": max(1, min(page_size, 100))}
        if page_token:
            params["pageToken"] = page_token
        return await self._request("GET", "notebooks", params=params)

    async def create_text_source(
        self,
        notebook_name: str,
        *,
        display_name: str,
        content: str,
        mime_type: str = "text/markdown",
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"{notebook_name}/sources",
            json={
                "display_name": display_name[:500],
                "mime_type": mime_type,
                "text_content": {"content": content},
            },
        )

    async def get_source(self, name: str) -> dict[str, Any]:
        return await self._request("GET", name)

    async def wait_source(self, name: str, *, timeout_sec: float) -> dict[str, Any]:
        """轮询到来源变为 ACTIVE / FAILED，或用完调用方给出的等待时间。"""
        source = await self.get_source(name)
        if _state(source) in _TERMINAL or timeout_sec <= 0:
            return source
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        delay = 1.5
        while _state(source) not in _TERMINAL:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(delay, remaining))
            source = await self.get_source(name)
            delay = min(delay * 1.5, 5)
        return source

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        if self._client is None:
            raise NotebookLmApiError("NotebookLM 客户端尚未打开")
        url = path if path.startswith("https://") else f"{API_ROOT}/{path.lstrip('/')}"
        response = await self._client.request(
            method,
            url,
            headers=self._headers(),
            json=json,
            params=params,
        )
        return _raise_for_status(response)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if self._project_id:
            headers["x-goog-user-project"] = self._project_id
        return headers


def _raise_for_status(response: httpx.Response) -> dict[str, Any]:
    data = _json_object(response)
    if response.status_code < 400:
        return data
    message = _google_message(data) or response.reason_phrase or "Gemini Notebook 请求失败"
    status = response.status_code
    lowered = message.lower()
    if status == 401:
        raise NotebookLmApiError("Google 授权已失效，请在设置里重新连接", status_code=401)
    if status == 403 and "workspace" in lowered:
        raise NotebookLmApiError(
            "当前公开预览不支持 Google Workspace 账号。请改用个人 Google 账号，或下载 Markdown / TXT 后在 Gemini Notebook 里手动添加来源。",
            status_code=400,
        )
    if status == 404 and "account not found" in lowered:
        raise NotebookLmApiError(
            "这个 Google 账号还没有打开过 Gemini Notebook。请先登录 https://notebook.google.com 一次，再重新导出。",
            status_code=400,
        )
    if status == 404:
        raise NotebookLmApiError("找不到这个 Gemini Notebook 或来源", status_code=404)
    if status == 429:
        raise NotebookLmApiError("Gemini Notebook 请求过于频繁，请稍后再试", status_code=429)
    if status == 400:
        raise NotebookLmApiError(f"Gemini Notebook 拒绝了这份来源：{_clip(message)}", status_code=400)
    raise NotebookLmApiError(f"Gemini Notebook 请求失败：{_clip(message)}", status_code=502)


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _google_message(data: dict[str, Any]) -> str:
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("status") or "")
    if isinstance(error, str):
        return error
    return str(data.get("message") or "")


def _state(source: dict[str, Any]) -> str:
    return str(source.get("state") or "")


def _clip(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
