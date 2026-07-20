"""ORCID OAuth — вход и привязка ORCID к профилю.

GET /auth/orcid/login → редирект в ORCID (scope /authenticate) →
GET /auth/orcid/callback → обмен code → токен(+orcid) → профиль pub.orcid.org →
привязка к профилю → редирект на /{locale}/researcher/{orcid}.

Два сценария в одном callback'е:
  • сессия есть  — привязываем ORCID к текущему аккаунту;
  • сессии нет   — заводим/находим аккаунт по ORCID и выдаём сессию (как Google).
Второй нужен потому, что кнопка на странице логина подписана «Войти через
ORCID»: раньше она разворачивала анонима обратно на логин с ошибкой
not_logged_in, и войти этим способом было нельзя вообще.

ORCID со scope /authenticate не отдаёт email — аккаунт создаётся без него
(users.email nullable), а связь держится через identities(provider='orcid').
"""
from __future__ import annotations

import urllib.parse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_optional_user
from src.core.config import settings
from src.core.cookies import set_auth_cookies
from src.core.security import create_access_token, create_refresh_token
from src.domain.auth import AuthDomain
from src.domain.orcid import OrcidDomain, OrcidTaken
from src.infrastructure.persistence.db import get_db
from src.infrastructure.persistence.models import User

router = APIRouter()
domain = OrcidDomain()
auth_domain = AuthDomain()


def _base() -> str:
    return (
        "https://sandbox.orcid.org"
        if settings.ORCID_ENV == "sandbox"
        else "https://orcid.org"
    )


def _pub_base() -> str:
    return (
        "https://pub.sandbox.orcid.org"
        if settings.ORCID_ENV == "sandbox"
        else "https://pub.orcid.org"
    )


def _require_config() -> None:
    if not (settings.ORCID_CLIENT_ID and settings.ORCID_CLIENT_SECRET and settings.ORCID_REDIRECT_URI):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ORCID OAuth is not configured (set ORCID_CLIENT_ID/SECRET/REDIRECT_URI)",
        )


@router.get("/login")
async def orcid_login(request: Request):
    _require_config()
    locale = request.query_params.get("locale", "ru")
    params = {
        "client_id": settings.ORCID_CLIENT_ID,
        "response_type": "code",
        "scope": "/authenticate",
        "redirect_uri": f"{settings.ORCID_REDIRECT_URI}?locale={locale}",
    }
    return RedirectResponse(_base() + "/oauth/authorize?" + urllib.parse.urlencode(params))


def _extract_profile(data: dict) -> dict:
    person = data.get("person") or {}
    name = person.get("name") or {}
    given = (name.get("given-names") or {}).get("value") or ""
    family = (name.get("family-name") or {}).get("value") or ""
    full_name = " ".join(x for x in (given, family) if x)

    activities = data.get("activities-summary") or {}
    emp = (activities.get("employments") or {}).get("employment-summary")
    workplace = ""
    if isinstance(emp, list) and emp:
        workplace = (emp[0].get("organization") or {}).get("name") or ""

    addresses = (person.get("addresses") or {}).get("address") or []
    country = ""
    if isinstance(addresses, list) and addresses:
        country = (addresses[0].get("country") or {}).get("value") or ""

    bio = (person.get("biography") or {}).get("content") or ""

    edu_summary = (activities.get("educations") or {}).get("education-summary")
    education = ""
    if isinstance(edu_summary, list) and edu_summary:
        edu = edu_summary[0]
        inst = (edu.get("organization") or {}).get("name") or ""
        start = ((edu.get("start-date") or {}).get("year") or {}).get("value") or ""
        end = ((edu.get("end-date") or {}).get("year") or {}).get("value") or "present"
        education = f"{inst} ({start} - {end})"

    return {
        "full_name": full_name,
        "workplace": workplace,
        "country": country,
        "bio": bio,
        "education": education,
    }


@router.get("/callback")
async def orcid_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    _require_config()
    locale = request.query_params.get("locale", "ru")
    front = settings.FRONTEND_URL.rstrip("/")

    def _login_err(code: str) -> RedirectResponse:
        return RedirectResponse(
            f"{front}/{locale}/auth/login?orcid_error={code}",
            status_code=status.HTTP_302_FOUND,
        )

    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing authorization code")

    redirect_uri = f"{settings.ORCID_REDIRECT_URI}?locale={locale}"
    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            _base() + "/oauth/token",
            data={
                "client_id": settings.ORCID_CLIENT_ID,
                "client_secret": settings.ORCID_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        if token_resp.status_code != 200:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "ORCID token exchange failed")
        tok = token_resp.json()
        orcid = tok.get("orcid")
        access_token = tok.get("access_token")
        if not orcid or not access_token:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing ORCID or access_token")

        prof_resp = await client.get(
            f"{_pub_base()}/v3.0/{orcid}",
            headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
        )
        profile_data = prof_resp.json() if prof_resp.status_code == 200 else {}

    fields = _extract_profile(profile_data)

    # Анонимный вход: находим аккаунт по этому ORCID или заводим новый и сразу
    # выдаём сессию. Без этого «Войти через ORCID» не может привести в профиль —
    # привязывать было бы не к чему.
    issue_session = user is None
    if issue_session:
        user = await auth_domain.get_or_create_oauth_user(
            db,
            provider="orcid",
            provider_id=orcid,
            email=None,
            full_name=fields.get("full_name") or None,
            identity_data={"orcid": orcid, **fields},
        )

    try:
        await domain.link_orcid(db, user_id=user.id, orcid=orcid, **fields)
    except OrcidTaken:
        # ORCID уже за другим аккаунтом. Для анонима это не ошибка входа: выше
        # мы нашли бы тот самый аккаунт по identity. Значит, ORCID привязан к
        # чужому профилю руками — просим войти под ним.
        return _login_err("orcid_taken")

    resp = RedirectResponse(
        f"{front}/{locale}/researcher/{orcid}", status_code=status.HTTP_302_FOUND
    )
    if issue_session:
        set_auth_cookies(
            resp,
            create_access_token(str(user.id), extra={"email": user.email}),
            create_refresh_token(str(user.id)),
        )
    return resp
