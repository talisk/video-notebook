"""用户与 Gemini Notebook（原 NotebookLM）的 OAuth 连接。

只存加密后的刷新令牌和短期访问令牌。客户端密钥留在环境变量，不进这张表。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

BigInt = BigInteger().with_variant(Integer, "sqlite")


class NotebookLmConnection(Base, TimestampMixin):
    __tablename__ = "notebooklm_connections"

    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)
    refresh_token_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    access_token_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    google_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scope: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    def set_refresh_token(self, raw: str) -> None:
        from app.core.crypto import encrypt

        self.refresh_token_enc = encrypt(raw) if raw else ""

    def get_refresh_token(self) -> str:
        from app.core.crypto import decrypt

        return decrypt(self.refresh_token_enc or "")

    def set_access_token(self, raw: str) -> None:
        from app.core.crypto import encrypt

        self.access_token_enc = encrypt(raw) if raw else ""

    def get_access_token(self) -> str:
        from app.core.crypto import decrypt

        return decrypt(self.access_token_enc or "")
