"""Обмен токена Turnstile на cookie-«пропуск человека».

Фронт зовёт этот эндпоинт лениво — когда защищённый роут ответил 403 с кодом
`turnstile_required`, — а не на каждой загрузке страницы.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from src.core.cookies import set_human_cookie
from src.core.ratelimit import limiter
from src.core.security import create_human_token
from src.core.turnstile import client_ip, verify_token
from src.schemas.security import TurnstileRequest, TurnstileResponse

router = APIRouter()


@router.post("/turnstile", response_model=TurnstileResponse)
@limiter.limit("20/minute")
async def exchange_turnstile_token(
    request: Request, body: TurnstileRequest, response: Response
):
    if not await verify_token(body.token, client_ip(request)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Turnstile verification failed"
        )
    set_human_cookie(response, create_human_token())
    return TurnstileResponse(ok=True)
