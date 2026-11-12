"""Gemini Notebook（原 NotebookLM）导出：文件回退、OAuth 与文本来源。"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.notebook import Subtitle, SubtitleLine, Video
from app.models.notebooklm import NotebookLmConnection
from app.models.user import User
from app.notebooklm import format as nb_format
from app.notebooklm.http import http_transport


class _Line:
    def __init__(self, text: str, start_ms: int | None) -> None:
        self.text = text
        self.start_ms = start_ms


def test_transcript_body_matches_existing_draft_lines():
    from app.services.draft_strategy import _transcript_from_lines

    lines = [
        _Line("  你好 ", 1500),
        _Line("   ", 2000),
        _Line("世界", 65_000),
        _Line("没有时间", None),
    ]
    assert nb_format.transcript_body(lines) == _transcript_from_lines(lines)
    assert nb_format.transcript_body(lines) == "[00:01] 你好\n[01:05] 世界\n没有时间"


def test_plain_export_uses_the_draft_wrapper():
    text = nb_format.render_plain(title="讲座", transcript="[00:01] 你好")
    assert text == "视频标题：讲座\n\n字幕：\n[00:01] 你好\n"


def test_markdown_is_split_under_the_source_limit(monkeypatch):
    monkeypatch.setattr(nb_format, "MAX_SOURCE_BYTES", 120)
    monkeypatch.setattr(nb_format, "MAX_SOURCE_WORDS", 10_000)
    transcript = "\n".join(f"[00:{i:02d}] 这是第{i}句转写内容" for i in range(12))
    sources = nb_format.build_markdown_sources(title="长视频", transcript=transcript)
    assert len(sources) > 1
    assert all(nb_format.fits_notebooklm_source(body) for _, body in sources)
    joined = "\n".join(body for _, body in sources)
    assert "这是第0句转写内容" in joined
    assert "这是第11句转写内容" in joined
    assert sources[0][0].endswith("（1/" + str(len(sources)) + "）") or "（1/" in sources[0][0]


def test_google_account_errors_stay_actionable():
    from app.notebooklm.client import NotebookLmApiError, _raise_for_status

    missing = httpx.Response(
        404,
        json={"error": {"message": "Account not found for user."}},
    )
    with pytest.raises(NotebookLmApiError) as missing_exc:
        _raise_for_status(missing)
    assert missing_exc.value.status_code == 400
    assert "notebook.google.com" in missing_exc.value.message

    denied = httpx.Response(401, json={"error": {"message": "invalid token"}})
    with pytest.raises(NotebookLmApiError) as denied_exc:
        _raise_for_status(denied)
    assert denied_exc.value.status_code == 401


def test_safe_filename_and_notebook_url():
    assert nb_format.safe_filename("a/b:c", "md") == "a b c.md"
    assert nb_format.notebook_web_url("notebooks/abc-123") == (
        "https://notebook.google.com/notebook/abc-123"
    )


@pytest.mark.asyncio
async def _user(db_session, phone: str = "13800000991") -> tuple[User, str]:
    user = User(phone=phone, name="导出", role="user", status="active")
    user.set_password("userpw1234")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    token, _ttl = create_access_token(user.id, role=user.role)
    return user, token


@pytest.mark.asyncio
async def _ready_video(db_session, owner: User, *, text: str = "你好，NotebookLM") -> Video:
    video = Video(
        owner_id=owner.id,
        source_type="url",
        source_url="https://www.youtube.com/watch?v=abcdefghijk",
        status="ready",
        title="讲座.mp4",
        subtitle_status="ready",
    )
    db_session.add(video)
    await db_session.commit()
    await db_session.refresh(video)
    subtitle = Subtitle(
        video_id=video.id,
        source="asr",
        language="zh",
        format="srt",
        line_count=1,
        version=1,
    )
    db_session.add(subtitle)
    await db_session.commit()
    await db_session.refresh(subtitle)
    db_session.add(
        SubtitleLine(
            subtitle_id=subtitle.id,
            line_no=1,
            start_ms=1500,
            end_ms=3000,
            text=text,
        )
    )
    await db_session.commit()
    return video


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _configure(monkeypatch, *, client_id: str = "client-id", secret: str = "super-secret") -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "notebooklm_oauth_client_id", client_id)
    monkeypatch.setattr(settings, "notebooklm_oauth_client_secret", secret)
    monkeypatch.setattr(settings, "notebooklm_project_id", "demo-project")
    monkeypatch.setattr(
        settings,
        "notebooklm_oauth_redirect_uri",
        "http://127.0.0.1:8000/api/notebooklm/oauth/callback",
    )


@pytest.mark.asyncio
async def test_file_export_without_google_credentials(client: AsyncClient, db_session):
    user, token = await _user(db_session)
    video = await _ready_video(db_session, user)

    markdown = await client.get(
        f"/api/sources/{video.id}/notebooklm/export.md",
        headers=_auth(token),
    )
    assert markdown.status_code == 200
    assert "text/markdown" in markdown.headers["content-type"]
    body = markdown.content.decode()
    assert "# 讲座.mp4" in body
    assert "[00:01] 你好，NotebookLM" in body
    assert "https://www.youtube.com/watch?v=abcdefghijk" in body
    assert "filename*=UTF-8''" in markdown.headers["content-disposition"]

    plain = await client.get(
        f"/api/sources/{video.id}/notebooklm/export.txt",
        headers=_auth(token),
    )
    assert plain.status_code == 200
    assert plain.content.decode() == "视频标题：讲座.mp4\n\n字幕：\n[00:01] 你好，NotebookLM\n"


@pytest.mark.asyncio
async def test_file_export_rejects_missing_transcript(client: AsyncClient, db_session):
    user, token = await _user(db_session, "13800000992")
    video = Video(
        owner_id=user.id,
        source_type="upload",
        status="ready",
        title="empty.mp4",
        subtitle_status="processing",
    )
    db_session.add(video)
    await db_session.commit()
    await db_session.refresh(video)
    resp = await client.get(
        f"/api/sources/{video.id}/notebooklm/export.md",
        headers=_auth(token),
    )
    assert resp.status_code == 400
    assert "生成" in resp.json()["message"]


@pytest.mark.asyncio
async def test_status_hides_secrets_and_reports_unconfigured(client: AsyncClient, db_session, monkeypatch):
    _configure(monkeypatch, client_id="", secret="super-secret")
    _user_token = await _user(db_session, "13800000993")
    _, token = _user_token
    resp = await client.get("/api/notebooklm/status", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["connected"] is False
    assert body["fallback"] == ["markdown", "txt"]
    assert body["redirect_uri"].endswith("/api/notebooklm/oauth/callback")
    raw = resp.text
    assert "super-secret" not in raw
    assert "client_secret" not in raw


@pytest.mark.asyncio
async def test_oauth_start_and_callback_store_encrypted_refresh_token(
    client: AsyncClient, db_session, monkeypatch
):
    _configure(monkeypatch)
    user, token = await _user(db_session, "13800000994")
    user_id = user.id
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        form = request.content.decode()
        assert "super-secret" in form
        assert "grant_type=authorization_code" in form
        return httpx.Response(
            200,
            json={
                "access_token": "ya29.access",
                "refresh_token": "1//refresh-token",
                "expires_in": 3600,
                "scope": "https://www.googleapis.com/auth/notebook",
                "token_type": "Bearer",
            },
        )

    transport = httpx.MockTransport(handler)
    var = http_transport.set(transport)
    try:
        start = await client.get(
            "/api/notebooklm/oauth/start",
            headers={**_auth(token), "Origin": "http://127.0.0.1:5173"},
            params={"next": "/settings?section=notebooklm"},
        )
        assert start.status_code == 200
        authorize = start.json()["authorize_url"]
        assert "super-secret" not in authorize
        query = parse_qs(urlsplit(authorize).query)
        assert query["scope"] == ["https://www.googleapis.com/auth/notebook"]
        assert query["redirect_uri"] == ["http://127.0.0.1:8000/api/notebooklm/oauth/callback"]
        state = query["state"][0]

        callback = await client.get(
            "/api/notebooklm/oauth/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
    finally:
        http_transport.reset(var)

    assert callback.status_code == 302
    location = callback.headers["location"]
    assert location.startswith("http://127.0.0.1:5173/settings?")
    assert "notebooklm=connected" in location
    assert "super-secret" not in location

    saved = (
        await db_session.execute(
            select(NotebookLmConnection).where(NotebookLmConnection.user_id == user_id)
        )
    ).scalar_one()
    assert saved.get_refresh_token() == "1//refresh-token"
    assert saved.refresh_token_enc != "1//refresh-token"
    assert "super-secret" not in saved.refresh_token_enc
    assert seen["url"].startswith("https://oauth2.googleapis.com/token")


@pytest.mark.asyncio
async def test_export_creates_notebook_and_markdown_source(
    client: AsyncClient, db_session, monkeypatch
):
    _configure(monkeypatch)
    user, token = await _user(db_session, "13800000995")
    video = await _ready_video(db_session, user)
    row = NotebookLmConnection(user_id=user.id, scope="https://www.googleapis.com/auth/notebook")
    row.set_refresh_token("1//refresh-token")
    row.set_access_token("ya29.cached")
    row.access_expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    db_session.add(row)
    await db_session.commit()

    calls: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        calls.append((request.method, str(request.url), payload if isinstance(payload, dict) else None))
        assert request.headers["authorization"] == "Bearer ya29.cached"
        assert request.headers["x-goog-user-project"] == "demo-project"
        if request.method == "POST" and request.url.path == "/v1/notebooks":
            return httpx.Response(
                200,
                json={"name": "notebooks/nb-1", "displayName": "讲座.mp4"},
            )
        if request.method == "POST" and request.url.path == "/v1/notebooks/nb-1/sources":
            return httpx.Response(
                200,
                json={
                    "name": "notebooks/nb-1/sources/src-1",
                    "displayName": "讲座.mp4",
                    "state": "ACTIVE",
                },
            )
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    var = http_transport.set(httpx.MockTransport(handler))
    try:
        resp = await client.post(
            f"/api/sources/{video.id}/notebooklm",
            headers=_auth(token),
            json={"target": "new", "notebook_title": "讲座笔记", "wait_seconds": 0},
        )
    finally:
        http_transport.reset(var)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["notebook_name"] == "notebooks/nb-1"
    assert body["notebook_url"] == "https://notebook.google.com/notebook/nb-1"
    assert body["sources"][0]["state"] == "ACTIVE"
    source_call = calls[1][2]
    assert source_call is not None
    assert source_call["mime_type"] == "text/markdown"
    assert "[00:01] 你好，NotebookLM" in source_call["text_content"]["content"]
    assert calls[0][2]["display_name"] == "讲座笔记"


@pytest.mark.asyncio
async def test_export_into_existing_notebook(client: AsyncClient, db_session, monkeypatch):
    _configure(monkeypatch)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.notebooklm.client.asyncio.sleep", _no_sleep)
    user, token = await _user(db_session, "13800000996")
    video = await _ready_video(db_session, user, text="已有笔记本")
    row = NotebookLmConnection(user_id=user.id)
    row.set_refresh_token("1//refresh-token")
    row.set_access_token("ya29.cached")
    row.access_expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    db_session.add(row)
    await db_session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/notebooks/existing":
            return httpx.Response(200, json={"name": "notebooks/existing", "displayName": "研究"})
        if request.method == "POST" and request.url.path.endswith("/sources"):
            return httpx.Response(
                200,
                json={"name": "notebooks/existing/sources/src-2", "state": "PROCESSING"},
            )
        if request.method == "GET" and "/sources/" in request.url.path:
            return httpx.Response(
                200,
                json={"name": "notebooks/existing/sources/src-2", "state": "ACTIVE"},
            )
        return httpx.Response(404, json={"error": {"message": str(request.url)}})

    var = http_transport.set(httpx.MockTransport(handler))
    try:
        resp = await client.post(
            f"/api/sources/{video.id}/notebooklm",
            headers=_auth(token),
            json={
                "target": "existing",
                "notebook_name": "notebooks/existing",
                "wait_seconds": 5,
            },
        )
    finally:
        http_transport.reset(var)

    assert resp.status_code == 200, resp.text
    assert resp.json()["sources"][0]["state"] == "ACTIVE"
    assert resp.json()["notebook_title"] == "研究"


@pytest.mark.asyncio
async def test_workspace_account_is_a_clear_error(client: AsyncClient, db_session, monkeypatch):
    _configure(monkeypatch)
    user, token = await _user(db_session, "13800000997")
    video = await _ready_video(db_session, user)
    row = NotebookLmConnection(user_id=user.id)
    row.set_refresh_token("1//refresh-token")
    row.set_access_token("ya29.cached")
    row.access_expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    db_session.add(row)
    await db_session.commit()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"status": "PERMISSION_DENIED", "message": "WORKSPACE_ACCOUNT_NOT_SUPPORTED"}},
        )

    var = http_transport.set(httpx.MockTransport(handler))
    try:
        resp = await client.post(
            f"/api/sources/{video.id}/notebooklm",
            headers=_auth(token),
            json={"target": "new", "wait_seconds": 0},
        )
    finally:
        http_transport.reset(var)

    assert resp.status_code == 400
    assert "Workspace" in resp.json()["message"]
    assert "Markdown" in resp.json()["message"]


@pytest.mark.asyncio
async def test_export_requires_connection(client: AsyncClient, db_session, monkeypatch):
    _configure(monkeypatch)
    user, token = await _user(db_session, "13800000998")
    video = await _ready_video(db_session, user)
    resp = await client.post(
        f"/api/sources/{video.id}/notebooklm",
        headers=_auth(token),
        json={"target": "new"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "notebooklm_not_connected"


@pytest.mark.asyncio
async def test_list_and_disconnect(client: AsyncClient, db_session, monkeypatch):
    _configure(monkeypatch)
    user, token = await _user(db_session, "13800000999")
    row = NotebookLmConnection(user_id=user.id)
    row.set_refresh_token("1//refresh-token")
    row.set_access_token("ya29.cached")
    row.access_expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    db_session.add(row)
    await db_session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["pageSize"] == "50"
        return httpx.Response(
            200,
            json={
                "notebooks": [
                    {"name": "notebooks/nb-9", "displayName": "已有"},
                ]
            },
        )

    var = http_transport.set(httpx.MockTransport(handler))
    try:
        listed = await client.get("/api/notebooklm/notebooks", headers=_auth(token))
    finally:
        http_transport.reset(var)
    assert listed.status_code == 200
    assert listed.json()["notebooks"][0]["url"] == "https://notebook.google.com/notebook/nb-9"

    gone = await client.post("/api/notebooklm/disconnect", headers=_auth(token))
    assert gone.status_code == 204
    status = await client.get("/api/notebooklm/status", headers=_auth(token))
    assert status.json()["connected"] is False


@pytest.mark.asyncio
async def test_refreshes_expired_access_token(client: AsyncClient, db_session, monkeypatch):
    _configure(monkeypatch)
    user, token = await _user(db_session, "13800000989")
    video = await _ready_video(db_session, user, text="需要刷新")
    row = NotebookLmConnection(user_id=user.id)
    row.set_refresh_token("1//refresh-token")
    row.set_access_token("ya29.stale")
    row.access_expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
    db_session.add(row)
    await db_session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            body = request.content.decode()
            assert "grant_type=refresh_token" in body
            assert "super-secret" in body
            return httpx.Response(200, json={"access_token": "ya29.fresh", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer ya29.fresh"
        if request.method == "POST" and request.url.path == "/v1/notebooks":
            return httpx.Response(200, json={"name": "notebooks/fresh", "displayName": "新"})
        if request.method == "POST" and request.url.path.endswith("/sources"):
            return httpx.Response(
                200,
                json={"name": "notebooks/fresh/sources/s", "state": "ACTIVE"},
            )
        return httpx.Response(404, json={"error": {"message": str(request.url)}})

    var = http_transport.set(httpx.MockTransport(handler))
    try:
        resp = await client.post(
            f"/api/sources/{video.id}/notebooklm",
            headers=_auth(token),
            json={"target": "new", "wait_seconds": 0},
        )
    finally:
        http_transport.reset(var)
    assert resp.status_code == 200, resp.text
    assert resp.json()["notebook_name"] == "notebooks/fresh"


@pytest.mark.asyncio
async def test_export_reports_missing_oauth_client(client: AsyncClient, db_session, monkeypatch):
    _configure(monkeypatch, client_id="", secret="")
    user, token = await _user(db_session, "13800000988")
    video = await _ready_video(db_session, user)
    resp = await client.post(
        f"/api/sources/{video.id}/notebooklm",
        headers=_auth(token),
        json={"target": "new"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "notebooklm_not_configured"


@pytest.mark.asyncio
async def test_rejects_notebook_name_injection(client: AsyncClient, db_session, monkeypatch):
    _configure(monkeypatch)
    user, token = await _user(db_session, "13800000990")
    video = await _ready_video(db_session, user)
    row = NotebookLmConnection(user_id=user.id)
    row.set_refresh_token("1//refresh-token")
    row.set_access_token("ya29.cached")
    row.access_expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    db_session.add(row)
    await db_session.commit()
    resp = await client.post(
        f"/api/sources/{video.id}/notebooklm",
        headers=_auth(token),
        json={"target": "existing", "notebook_name": "notebooks/../../admin"},
    )
    assert resp.status_code == 400
