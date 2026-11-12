"""把字幕转写导出到 Gemini Notebook（原 NotebookLM），或下载可手动导入的文件。"""
from __future__ import annotations

from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_active_user
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.session import get_session
from app.models.user import User
from app.notebooklm.oauth import (
    NotebookLmOAuthError,
    build_authorize_url,
    decode_oauth_state,
    encode_oauth_state,
    oauth_configured,
    origin_allowed,
    redirect_uri_for,
    safe_next_path,
)
from app.schemas.notebooklm import (
    NotebookLmAuthorize,
    NotebookLmExportRequest,
    NotebookLmExportResult,
    NotebookLmNotebookList,
    NotebookLmStatus,
)
from app.services import notebooklm_export as svc

router = APIRouter(tags=["notebooklm"])
logger = get_logger("notebooklm")


@router.get("/notebooklm/status", response_model=NotebookLmStatus)
async def notebooklm_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_active_user),
):
    status = await svc.connection_status(session, user.id)
    status["redirect_uri"] = redirect_uri_for(str(request.base_url))
    return status


@router.get("/notebooklm/oauth/start", response_model=NotebookLmAuthorize)
async def notebooklm_oauth_start(
    request: Request,
    next: str | None = Query(default=None),
    user: User = Depends(get_current_active_user),
):
    if not oauth_configured():
        raise svc.NotebookLmNotConfiguredError()
    redirect_uri = redirect_uri_for(str(request.base_url))
    origin = _return_origin(request)
    state = encode_oauth_state(
        user_id=user.id,
        origin=origin,
        next_path=safe_next_path(next),
        redirect_uri=redirect_uri,
    )
    return {"authorize_url": build_authorize_url(state=state, redirect_uri=redirect_uri)}


@router.get("/notebooklm/oauth/callback")
async def notebooklm_oauth_callback(
    request: Request,
    session: AsyncSession = Depends(get_session),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Google 把浏览器带回这里。没有登录 JWT，身份在签名过的 state 里。"""
    origin = ""
    next_path = "/settings?section=notebooklm"
    try:
        if error:
            raise NotebookLmOAuthError(_oauth_denied_message(error))
        if not code or not state:
            raise NotebookLmOAuthError("Google 没有返回授权码，请重新连接")
        payload = decode_oauth_state(state)
        origin = str(payload.get("origin") or "")
        next_path = safe_next_path(str(payload.get("next") or ""))
        user_id = int(payload["sub"])
        redirect_uri = str(payload.get("redirect_uri") or redirect_uri_for(str(request.base_url)))
        await svc.finish_oauth_code(
            session,
            user_id=user_id,
            code=code,
            redirect_uri=redirect_uri,
        )
    except AppError as exc:
        return _finish_redirect(
            origin, next_path, notebooklm="error", message=_clip(exc.message)
        )
    except (KeyError, TypeError, ValueError):
        return _finish_redirect(
            origin, next_path, notebooklm="error", message="授权状态无效，请重新连接 Google"
        )
    except Exception:
        logger.exception("notebooklm oauth callback failed")
        return _finish_redirect(
            origin, next_path, notebooklm="error", message="连接 Google 失败，请重试"
        )
    return _finish_redirect(origin, next_path, notebooklm="connected")


@router.post("/notebooklm/disconnect", status_code=204)
async def notebooklm_disconnect(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_active_user),
):
    await svc.disconnect(session, user.id)
    return Response(status_code=204)


@router.get("/notebooklm/notebooks", response_model=NotebookLmNotebookList)
async def notebooklm_notebooks(
    page_token: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_active_user),
):
    return await svc.list_remote_notebooks(session, user_id=user.id, page_token=page_token)


@router.post("/sources/{video_id}/notebooklm", response_model=NotebookLmExportResult)
async def export_source_to_notebooklm(
    video_id: int,
    body: NotebookLmExportRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_active_user),
):
    return await svc.export_transcript(
        session,
        viewer=user,
        video_id=video_id,
        target=body.target,
        notebook_title=body.notebook_title,
        notebook_name=body.notebook_name,
        wait_seconds=body.wait_seconds,
    )


@router.get("/sources/{video_id}/notebooklm/export.md")
async def export_source_markdown(
    video_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_active_user),
):
    payload, filename, media = await svc.export_file(
        session, viewer_id=user.id, video_id=video_id, kind="md"
    )
    return _file(payload, filename, media)


@router.get("/sources/{video_id}/notebooklm/export.txt")
async def export_source_text(
    video_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_active_user),
):
    payload, filename, media = await svc.export_file(
        session, viewer_id=user.id, video_id=video_id, kind="txt"
    )
    return _file(payload, filename, media)


def _file(payload: bytes, filename: str, media: str) -> Response:
    ascii_name = "transcript" + (".md" if filename.endswith(".md") else ".txt")
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return Response(content=payload, media_type=media, headers={"Content-Disposition": disposition})


def _return_origin(request: Request) -> str:
    candidates: list[str] = []
    origin = request.headers.get("origin")
    if origin:
        candidates.append(origin)
    referer = request.headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        if parts.scheme and parts.netloc:
            candidates.append(f"{parts.scheme}://{parts.netloc}")
    host = request.url.hostname
    for candidate in candidates:
        if origin_allowed(candidate, request_host=host):
            return candidate.rstrip("/")
    from app.core.config import get_settings

    settings = get_settings()
    for base in (settings.web_origin, settings.public_base_url):
        if base and origin_allowed(base, request_host=host):
            return base.rstrip("/")
    return ""


def _finish_redirect(origin: str, next_path: str, **params: str) -> Response:
    target_path = svc.with_query(next_path, **params)
    if origin and origin_allowed(origin):
        return RedirectResponse(f"{origin.rstrip('/')}{target_path}", status_code=302)
    title = "已连接 Gemini Notebook" if params.get("notebooklm") == "connected" else "连接未完成"
    detail = params.get("message") or (
        "可以回到视频笔记本，把转写发送到 Gemini Notebook。"
        if params.get("notebooklm") == "connected"
        else "请回到视频笔记本的设置页重试。"
    )
    body = (
        "<!doctype html><meta charset=\"utf-8\">"
        f"<title>{_html(title)}</title>"
        "<body style=\"font-family:sans-serif;max-width:36rem;margin:4rem auto;line-height:1.5\">"
        f"<h1>{_html(title)}</h1><p>{_html(detail)}</p>"
        "<p>Gemini Notebook（原 NotebookLM）："
        "<a href=\"https://notebook.google.com\">notebook.google.com</a></p>"
        "</body>"
    )
    return HTMLResponse(body)


def _oauth_denied_message(error: str) -> str:
    if error == "access_denied":
        return "你取消了 Google 授权"
    return "Google 授权被拒绝，请重试"


def _html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _clip(text: str, limit: int = 180) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
