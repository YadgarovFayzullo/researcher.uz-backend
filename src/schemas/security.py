from __future__ import annotations

from pydantic import BaseModel


class TurnstileRequest(BaseModel):
    """Токен виджета Cloudflare Turnstile (cf-turnstile-response)."""

    token: str


class TurnstileResponse(BaseModel):
    ok: bool
