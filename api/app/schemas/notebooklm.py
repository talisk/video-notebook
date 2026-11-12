"""Gemini Notebook 导出的请求与响应。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class NotebookLmStatus(BaseModel):
    configured: bool
    project_id_set: bool
    redirect_uri: str
    connected: bool
    google_email: str | None = None
    api: str = "gemini_notebook"
    product_url: str = "https://notebook.google.com"
    fallback: list[str] = Field(default_factory=lambda: ["markdown", "txt"])


class NotebookLmAuthorize(BaseModel):
    authorize_url: str


class NotebookLmNotebook(BaseModel):
    name: str
    display_name: str
    url: str


class NotebookLmNotebookList(BaseModel):
    notebooks: list[NotebookLmNotebook]
    next_page_token: str | None = None


class NotebookLmExportRequest(BaseModel):
    target: Literal["new", "existing"] = "new"
    notebook_title: str | None = Field(default=None, max_length=500)
    notebook_name: str | None = Field(default=None, max_length=200)
    wait_seconds: int = Field(default=8, ge=0, le=30)


class NotebookLmSourceResult(BaseModel):
    name: str
    display_name: str
    state: str
    error: str | None = None


class NotebookLmExportResult(BaseModel):
    notebook_name: str
    notebook_title: str
    notebook_url: str
    sources: list[NotebookLmSourceResult]
