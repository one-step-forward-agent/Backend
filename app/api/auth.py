from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from urllib.parse import quote, urlencode

import httpx
import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ACCESS_COOKIE, REFRESH_COOKIE, get_current_user
from app.core import ratelimit
from app.core.auth import create_access_token, decode_access_token, create_refresh_token, decode_refresh_token, hash_password, verify_password
from app.core.config import settings
from app.core.database import get_session
from app.core.dataenc import email_lookup
from app.models.models import Calendar, Integration, RefreshToken, User
from app.services.integrations.service import integration_secrets, store_secrets
from app.schemas import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse

session_router = APIRouter(prefix="/auth", tags=["auth"])
google_router = APIRouter(prefix="/auth/google", tags=["auth"])
yandex_router = APIRouter(prefix="/auth/yandex", tags=["auth"])
apple_router = APIRouter(prefix="/auth/apple", tags=["auth"])
notion_router = APIRouter(prefix="/auth/notion", tags=["auth"])
jira_router = APIRouter(prefix="/auth/jira", tags=["auth"])
obsidian_router = APIRouter(prefix="/auth/obsidian", tags=["auth"])

LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES_PER_EMAIL = 10
# Many emails from one address (password spraying)
LOGIN_MAX_FAILURES_PER_IP = 30
REGISTER_WINDOW_SECONDS = 60 * 60
REGISTER_MAX_PER_IP = 10
REFRESH_REUSE_GRACE = timedelta(seconds=30)


async def _token_response(session: AsyncSession, user_id: int, cookies: bool = True) -> JSONResponse:
    now = datetime.now(timezone.utc)
    await session.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.expires_at < now))
    access_token = create_access_token(user_id)
    refresh_token, jti, expires_at = create_refresh_token(user_id)
    session.add(RefreshToken(jti=jti, user_id=user_id, expires_at=expires_at))
    await session.commit()
    body = TokenResponse(access_token=access_token, refresh_token=refresh_token, expires_in=settings.jwt_expire_minutes * 60)
    response = JSONResponse(body.model_dump())
    if not cookies:
        return response
    response.set_cookie(
        ACCESS_COOKIE, access_token, httponly=True, samesite="lax", secure=settings.cookie_secure, max_age=settings.jwt_expire_minutes * 60
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        httponly=True,
        samesite="strict",
        secure=settings.cookie_secure,
        max_age=settings.jwt_refresh_expire_days * 86400,
        path="/auth",
    )
    return response


async def _authenticate(session: AsyncSession, email: str, password: str, ip: str) -> User:
    email_key, ip_key = f"login:email:{email}", f"login:ip:{ip}"
    if ratelimit.is_limited(email_key, LOGIN_MAX_FAILURES_PER_EMAIL, LOGIN_WINDOW_SECONDS) or ratelimit.is_limited(
        ip_key, LOGIN_MAX_FAILURES_PER_IP, LOGIN_WINDOW_SECONDS
    ):
        raise HTTPException(status_code=429, detail="Слишком много попыток входа, попробуйте через 15 минут")
    user = await session.scalar(select(User).where(User.email_hash.in_(email_lookup(email)), User.is_active.is_(True)))
    if not user or not verify_password(password, user.password_hash):
        ratelimit.record(email_key)
        ratelimit.record(ip_key)
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    ratelimit.reset(email_key)
    return user


@session_router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, request: Request, session: AsyncSession = Depends(get_session)):
    ratelimit.hit(
        f"register:{ratelimit.client_ip(request)}",
        REGISTER_MAX_PER_IP,
        REGISTER_WINDOW_SECONDS,
        "Слишком много регистраций с вашего адреса, попробуйте позже",
    )
    if await session.scalar(select(User.id).where(User.email_hash.in_(email_lookup(payload.email)))):
        raise HTTPException(status_code=409, detail="Пользователь с таким email уже существует")
    user = User(
        email=payload.email,
        name=payload.name,
        timezone=payload.timezone or settings.default_timezone,
        password_hash=hash_password(payload.password),
        is_active=True,
    )
    session.add(user)
    await session.commit()
    response = await _token_response(session, user.id)
    response.status_code = status.HTTP_201_CREATED
    return response


@session_router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request, session: AsyncSession = Depends(get_session)):
    user = await _authenticate(session, payload.email, payload.password, ratelimit.client_ip(request))
    return await _token_response(session, user.id)


@session_router.post("/token", response_model=TokenResponse, include_in_schema=True)
async def token(request: Request, form: OAuth2PasswordRequestForm = Depends(), session: AsyncSession = Depends(get_session)):
    user = await _authenticate(session, form.username.strip().lower(), form.password, ratelimit.client_ip(request))
    return await _token_response(session, user.id, cookies=False)


def _session_expired() -> HTTPException:
    return HTTPException(status_code=401, detail="Сессия истекла, войдите снова")


async def _revoke_all(session: AsyncSession, user_id: int, now: datetime) -> None:
    await session.execute(
        update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)).values(revoked_at=now)
    )


@session_router.post("/refresh", response_model=TokenResponse)
async def refresh(
    payload: RefreshRequest | None = None,
    cookie_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    session: AsyncSession = Depends(get_session),
):
    token = (payload.refresh_token if payload else None) or cookie_token
    try:
        user_id, jti = decode_refresh_token(token or "")
    except (jwt.InvalidTokenError, ValueError, KeyError):
        raise _session_expired() from None
    stored = await session.get(RefreshToken, jti)
    if not stored or stored.user_id != user_id:
        raise _session_expired()
    now = datetime.now(timezone.utc)
    if stored.revoked_at is not None:
        raise _session_expired()
    if stored.rotated_at is None:
        stored.rotated_at = now
    elif now - stored.rotated_at > REFRESH_REUSE_GRACE:
        await _revoke_all(session, user_id, now)
        await session.commit()
        raise _session_expired()
    user = await session.get(User, user_id)
    if not user or not user.is_active:
        await session.commit()
        raise HTTPException(status_code=401, detail="Пользователь не найден или отключён")
    return await _token_response(session, user.id)


def _logout_response() -> JSONResponse:
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie(ACCESS_COOKIE)
    response.delete_cookie(REFRESH_COOKIE, path="/auth")
    return response


@session_router.post("/logout")
async def logout(
    payload: RefreshRequest | None = None,
    cookie_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    session: AsyncSession = Depends(get_session),
):
    token = (payload.refresh_token if payload else None) or cookie_token
    try:
        _, jti = decode_refresh_token(token or "")
    except (jwt.InvalidTokenError, ValueError, KeyError):
        return _logout_response()
    await session.execute(
        update(RefreshToken).where(RefreshToken.jti == jti, RefreshToken.revoked_at.is_(None)).values(revoked_at=datetime.now(timezone.utc))
    )
    await session.commit()
    return _logout_response()


@session_router.post("/logout-all")
async def logout_all(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await _revoke_all(session, user.id, datetime.now(timezone.utc))
    await session.commit()
    return _logout_response()


def safe_return_path(path: str | None) -> str | None:
    """Only same-site relative paths are allowed as OAuth return targets (no open redirects)."""
    if not path or len(path) > 300 or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return None
    return path


def _oauth_state(user_id: int, return_to: str | None = None) -> str:
    data = {"user_id": user_id, "expires": int(datetime.now(timezone.utc).timestamp()) + 600}
    if safe_return_path(return_to):
        data["return_to"] = return_to
    payload = urlsafe_b64encode(json.dumps(data).encode()).decode()
    signature = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _validate_oauth_state(state: str) -> tuple[int, str | None]:
    try:
        payload, signature = state.split(".", 1)
        expected = hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        data = json.loads(urlsafe_b64decode(payload.encode()))
        if int(data["expires"]) < int(datetime.now(timezone.utc).timestamp()):
            raise ValueError
        return int(data["user_id"]), safe_return_path(data.get("return_to"))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state") from None


def google_authorization_url(user_id: int, return_to: str | None = None) -> str:
    if not settings.google_client_id:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile https://www.googleapis.com/auth/calendar",
            "access_type": "offline",
            "prompt": "select_account consent",
            "state": _oauth_state(user_id, return_to),
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"

def yandex_authorization_url(user_id: int, return_to: str | None = None) -> str:
    """Формирует ссылку на страницу авторизации Яндекс ID."""
    if not settings.yandex_client_id:
        raise HTTPException(status_code=503, detail="Yandex OAuth is not configured")
    query = urlencode(
        {
            "client_id": settings.yandex_client_id,
            "redirect_uri": settings.yandex_redirect_uri,
            "response_type": "code",
            "scope": "calendar:all",
            "state": _oauth_state(user_id, return_to),
        }
    )
    return f"https://oauth.yandex.ru/authorize?{query}"

NOTION_VERSION = "2022-06-28"


def notion_authorization_url(user_id: int, return_to: str | None = None) -> str:
    """Формирует ссылку на страницу авторизации Notion OAuth."""
    if not settings.notion_client_id:
        raise HTTPException(status_code=503, detail="Notion OAuth is not configured")
    query = urlencode(
        {
            "client_id": settings.notion_client_id,
            "redirect_uri": settings.notion_redirect_uri,
            "response_type": "code",
            "owner": "user",
            "state": _oauth_state(user_id, return_to),
        }
    )
    return f"https://api.notion.com/v1/oauth/authorize?{query}"


JIRA_SCOPES = "read:jira-work read:jira-user offline_access read:me"


def jira_authorization_url(user_id: int, return_to: str | None = None) -> str:
    """Формирует ссылку на страницу авторизации Atlassian OAuth 2.0 (3LO)."""
    if not settings.jira_client_id:
        raise HTTPException(status_code=503, detail="Jira OAuth is not configured")
    query = urlencode(
        {
            "audience": "api.atlassian.com",
            "client_id": settings.jira_client_id,
            "scope": JIRA_SCOPES,
            "redirect_uri": settings.jira_redirect_uri,
            "state": _oauth_state(user_id, return_to),
            "response_type": "code",
            "prompt": "consent",
        }
    )
    return f"https://auth.atlassian.com/authorize?{query}"


@google_router.get("/login")
async def google_login(user: User = Depends(get_current_user)):
    return {"authorization_url": google_authorization_url(user.id)}

@yandex_router.get("/login")
async def yandex_login(user: User = Depends(get_current_user)):
    return {"authorization_url": yandex_authorization_url(user.id)}

@notion_router.get("/login")
async def notion_login(user: User = Depends(get_current_user)):
    return {"authorization_url": notion_authorization_url(user.id)}


@jira_router.get("/login")
async def jira_login(user: User = Depends(get_current_user)):
    return {"authorization_url": jira_authorization_url(user.id)}


@google_router.get("/callback")
async def google_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    access_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    session: AsyncSession = Depends(get_session),
):
    if error:
        raise HTTPException(status_code=400, detail=f"Google OAuth failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth authorization code")
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    user_id, return_to = _validate_oauth_state(state)
    # The Google account is linked only to the Dayla account that is logged in in this browser.
    # Otherwise an attacker could send a victim their own authorization link and receive the
    # victim's calendar in the attacker's account.
    try:
        session_user_id = decode_access_token(access_cookie or "")
    except (jwt.InvalidTokenError, ValueError):
        session_user_id = None
    if session_user_id is None:
        next_path = return_to or "/app/integrations"
        return RedirectResponse(url=f"/login?next={quote(next_path, safe='/')}", status_code=303)
    if session_user_id != user_id:
        raise HTTPException(status_code=403, detail="Ссылка подключения Google создана для другого аккаунта Dayla")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if response.is_error:
        raise HTTPException(status_code=400, detail="Google rejected the authorization code")
    token_data = response.json()
    async with httpx.AsyncClient(timeout=15) as client:
        profile_response = await client.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
    if profile_response.is_error or not profile_response.json().get("email"):
        raise HTTPException(status_code=400, detail="Google account email was not returned")
    profile = profile_response.json()
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь Dayla не найден")
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user_id, Integration.provider == "google")
    )
    expires_in = token_data.get("expires_in")
    values = {
        "user_id": user_id,
        "provider": "google",
        "account_email": profile["email"],
        "token_expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_in) if expires_in else None,
        "status": "connected",
        "last_sync_error": None,
    }
    if integration:
        for key, value in values.items():
            setattr(integration, key, value)
    else:
        integration = Integration(config={}, **values)
        session.add(integration)
    tokens = {"access_token": token_data["access_token"]}
    refresh_token = token_data.get("refresh_token") or integration_secrets(integration).get("refresh_token")
    if refresh_token:
        tokens["refresh_token"] = refresh_token
    store_secrets(integration, tokens)
    await session.flush()
    google_calendar = await session.scalar(
        select(Calendar).where(Calendar.user_id == user_id, Calendar.provider == "google", Calendar.external_id == "primary")
    )
    if not google_calendar:
        session.add(Calendar(user_id=user_id, integration_id=integration.id, name="Google Calendar", provider="google", external_id="primary", timezone="UTC"))
    await session.commit()
    if return_to:
        separator = "&" if "?" in return_to else "?"
        return RedirectResponse(url=f"{return_to}{separator}connected=google", status_code=303)
    return RedirectResponse(url="/?connected=google", status_code=303)


@yandex_router.get("/callback")
async def yandex_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    access_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    session: AsyncSession = Depends(get_session),
):
    if error:
        raise HTTPException(status_code=400, detail=f"Yandex OAuth failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth authorization code")
    if not settings.yandex_client_id or not settings.yandex_client_secret:
        raise HTTPException(status_code=503, detail="Yandex OAuth is not configured")

    user_id, return_to = _validate_oauth_state(state)
    try:
        session_user_id = decode_access_token(access_cookie or "")
    except (jwt.InvalidTokenError, ValueError):
        session_user_id = None
    if session_user_id is None:
        next_path = return_to or "/app/integrations"
        return RedirectResponse(url=f"/login?next={quote(next_path, safe='/')}", status_code=303)
    if session_user_id != user_id:
        raise HTTPException(status_code=403, detail="Ссылка подключения Яндекс создана для другого аккаунта Dayla")

    async with httpx.AsyncClient(timeout=15) as client:
        token_response = await client.post(
            "https://oauth.yandex.ru/token",
            data={
                "code": code,
                "client_id": settings.yandex_client_id,
                "client_secret": settings.yandex_client_secret,
                "redirect_uri": settings.yandex_redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if token_response.is_error:
        raise HTTPException(status_code=400, detail="Yandex rejected the authorization code")
    token_data = token_response.json()

    async with httpx.AsyncClient(timeout=15) as client:
        profile_response = await client.get(
            "https://login.yandex.ru/info",
            headers={"Authorization": f"OAuth {token_data['access_token']}"},
            params={"format": "json"},
        )
    if profile_response.is_error:
        raise HTTPException(status_code=400, detail="Failed to fetch Yandex user profile")
    profile = profile_response.json()
    email = profile.get("default_email") or (profile.get("emails") or [None])[0]
    if not email:
        raise HTTPException(status_code=400, detail="Yandex account email was not returned")

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь Dayla не найден")

    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user_id, Integration.provider == "yandex")
    )
    expires_in = token_data.get("expires_in")
    values = {
        "user_id": user_id,
        "provider": "yandex",
        "account_email": email,
        "token_expires_at": datetime.now(timezone.utc) + timedelta(seconds=expires_in) if expires_in else None,
        "status": "connected",
        "last_sync_error": None,
    }
    if integration:
        for key, value in values.items():
            setattr(integration, key, value)
    else:
        integration = Integration(config={}, **values)
        session.add(integration)

    tokens = {"access_token": token_data["access_token"]}
    if token_data.get("refresh_token"):
        tokens["refresh_token"] = token_data["refresh_token"]
    store_secrets(integration, tokens)

    await session.flush()

    yandex_calendar = await session.scalar(
        select(Calendar).where(Calendar.user_id == user_id, Calendar.provider == "yandex")
    )
    if not yandex_calendar:
        session.add(Calendar(
            user_id=user_id,
            integration_id=integration.id,
            name="Yandex Calendar",
            provider="yandex",
            external_id="placeholder",
            timezone="UTC",
        ))

    await session.commit()

    if return_to:
        separator = "&" if "?" in return_to else "?"
        return RedirectResponse(url=f"{return_to}{separator}connected=yandex", status_code=303)
    return RedirectResponse(url="/?connected=yandex", status_code=303)


@notion_router.get("/callback")
async def notion_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    access_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    session: AsyncSession = Depends(get_session),
):
    if error:
        raise HTTPException(status_code=400, detail=f"Notion OAuth failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth authorization code")
    if not settings.notion_client_id or not settings.notion_client_secret:
        raise HTTPException(status_code=503, detail="Notion OAuth is not configured")

    user_id, return_to = _validate_oauth_state(state)

    # Та же проверка, что и в Google/Yandex:
    # подключение возможно только для аккаунта, залогиненного в браузере.
    try:
        session_user_id = decode_access_token(access_cookie or "")
    except (jwt.InvalidTokenError, ValueError):
        session_user_id = None
    if session_user_id is None:
        next_path = return_to or "/app/integrations"
        return RedirectResponse(url=f"/login?next={quote(next_path, safe='/')}", status_code=303)
    if session_user_id != user_id:
        raise HTTPException(status_code=403, detail="Ссылка подключения Notion создана для другого аккаунта Dayla")

    # 1. Обмен authorization code на access_token.
    #    Notion требует Basic Auth: base64(client_id:client_secret).
    import base64
    basic = base64.b64encode(
        f"{settings.notion_client_id}:{settings.notion_client_secret}".encode()
    ).decode()

    async with httpx.AsyncClient(timeout=15) as client:
        token_response = await client.post(
            "https://api.notion.com/v1/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.notion_redirect_uri,
            },
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/json",
                "Notion-Version": NOTION_VERSION,
            },
        )
    if token_response.is_error:
        raise HTTPException(status_code=400, detail="Notion rejected the authorization code")
    token_data = token_response.json()

    # 2. Забираем профиль бота и workspace.
    async with httpx.AsyncClient(timeout=15) as client:
        profile_response = await client.get(
            "https://api.notion.com/v1/users/me",
            headers={
                "Authorization": f"Bearer {token_data['access_token']}",
                "Notion-Version": NOTION_VERSION,
            },
        )
    if profile_response.is_error:
        raise HTTPException(status_code=400, detail="Failed to fetch Notion user profile")
    profile = profile_response.json()

    # email может отсутствовать, если у бота нет person-владельца.
    # Тогда используем имя воркспейса или bot_id как account_email.
    person = profile.get("person") or {}
    email = person.get("email")
    if not email:
        bot = profile.get("bot") or {}
        workspace = bot.get("workspace_name") or "Notion Workspace"
        email = f"{workspace} ({profile.get('id', 'unknown')})"

    # 3. Сохраняем интеграцию.
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь Dayla не найден")

    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user_id, Integration.provider == "notion")
    )
    values = {
        "user_id": user_id,
        "provider": "notion",
        "account_email": email[:320],
        "status": "connected",
        "last_sync_error": None,
    }
    if integration:
        for key, value in values.items():
            setattr(integration, key, value)
    else:
        integration = Integration(config={}, **values)
        session.add(integration)

    # 4. Токены — в зашифрованное хранилище.
    tokens = {"access_token": token_data["access_token"]}
    if token_data.get("refresh_token"):
        tokens["refresh_token"] = token_data["refresh_token"]
    store_secrets(integration, tokens)

    await session.flush()

    # 5. Календарь-плейсхолдер.
    notion_calendar = await session.scalar(
        select(Calendar).where(Calendar.user_id == user_id, Calendar.provider == "notion")
    )
    if not notion_calendar:
        session.add(Calendar(
            user_id=user_id,
            integration_id=integration.id,
            name="Notion",
            provider="notion",
            external_id=profile.get("id", "notion-workspace"),
            timezone="UTC",
        ))

    await session.commit()

    if return_to:
        separator = "&" if "?" in return_to else "?"
        return RedirectResponse(url=f"{return_to}{separator}connected=notion", status_code=303)
    return RedirectResponse(url="/?connected=notion", status_code=303)


@jira_router.get("/callback")
async def jira_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    access_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    session: AsyncSession = Depends(get_session),
):
    if error:
        raise HTTPException(status_code=400, detail=f"Jira OAuth failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth authorization code")
    if not settings.jira_client_id or not settings.jira_client_secret:
        raise HTTPException(status_code=503, detail="Jira OAuth is not configured")

    user_id, return_to = _validate_oauth_state(state)

    try:
        session_user_id = decode_access_token(access_cookie or "")
    except (jwt.InvalidTokenError, ValueError):
        session_user_id = None
    if session_user_id is None:
        next_path = return_to or "/app/integrations"
        return RedirectResponse(url=f"/login?next={quote(next_path, safe='/')}", status_code=303)
    if session_user_id != user_id:
        raise HTTPException(status_code=403, detail="Ссылка подключения Jira создана для другого аккаунта Dayla")

    # 1. Обмен authorization code на access_token.
    async with httpx.AsyncClient(timeout=15) as client:
        token_response = await client.post(
            "https://auth.atlassian.com/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": settings.jira_client_id,
                "client_secret": settings.jira_client_secret,
                "code": code,
                "redirect_uri": settings.jira_redirect_uri,
            },
            headers={"Content-Type": "application/json"},
        )
    if token_response.is_error:
        raise HTTPException(status_code=400, detail="Atlassian rejected the authorization code")
    token_data = token_response.json()

    # 2. Получаем список сайтов (cloudid), к которым дан доступ.
    async with httpx.AsyncClient(timeout=15) as client:
        resources_response = await client.get(
            "https://api.atlassian.com/oauth/token/accessible-resources",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
    if resources_response.is_error:
        raise HTTPException(status_code=400, detail="Failed to fetch accessible Jira resources")
    resources = resources_response.json()
    if not resources:
        raise HTTPException(status_code=400, detail="No accessible Jira sites found")
    # Берём первый сайт (Resource-level доступ)
    cloud_id = resources[0]["id"]
    site_url = resources[0].get("url", "")
    site_name = resources[0].get("name", "Jira")

    # 3. Профиль пользователя.
    async with httpx.AsyncClient(timeout=15) as client:
        profile_response = await client.get(
            "https://api.atlassian.com/me",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
    if profile_response.is_error:
        raise HTTPException(status_code=400, detail="Failed to fetch Atlassian user profile")
    profile = profile_response.json()
    email = profile.get("email")
    if not email:
        email = f"{site_name} ({cloud_id})"

    # 4. Сохраняем интеграцию.
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь Dayla не найден")

    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user_id, Integration.provider == "jira")
    )
    values = {
        "user_id": user_id,
        "provider": "jira",
        "account_email": email[:320],
        "status": "connected",
        "last_sync_error": None,
    }
    if integration:
        for key, value in values.items():
            setattr(integration, key, value)
    else:
        integration = Integration(config={}, **values)
        session.add(integration)

    # 5. Токены — в зашифрованное хранилище.
    tokens = {"access_token": token_data["access_token"]}
    if token_data.get("refresh_token"):
        tokens["refresh_token"] = token_data["refresh_token"]
    store_secrets(integration, tokens)

    # 6. Сохраняем cloud_id и site_url в config интеграции.
    integration.config = {
        **(integration.config or {}),
        "cloud_id": cloud_id,
        "site_url": site_url,
        "site_name": site_name,
    }

    await session.flush()

    jira_calendar = await session.scalar(
        select(Calendar).where(Calendar.user_id == user_id, Calendar.provider == "jira")
    )
    if not jira_calendar:
        session.add(Calendar(
            user_id=user_id,
            integration_id=integration.id,
            name=site_name,
            provider="jira",
            external_id=cloud_id,
            timezone="UTC",
        ))

    await session.commit()

    if return_to:
        separator = "&" if "?" in return_to else "?"
        return RedirectResponse(url=f"{return_to}{separator}connected=jira", status_code=303)
    return RedirectResponse(url="/?connected=jira", status_code=303)


@google_router.post("/disconnect")
async def google_disconnect(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user.id, Integration.provider == "google")
    )
    if integration:
        await session.delete(integration)
        await session.commit()
    return {"status": "disconnected"}


@yandex_router.post("/disconnect")
async def yandex_disconnect(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user.id, Integration.provider == "yandex")
    )
    if integration:
        await session.delete(integration)
        await session.commit()
    return {"status": "disconnected"}


@notion_router.post("/disconnect")
async def notion_disconnect(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user.id, Integration.provider == "notion")
    )
    if integration:
        await session.delete(integration)
        await session.commit()
    return {"status": "disconnected"}


@jira_router.post("/disconnect")
async def jira_disconnect(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    integration = await session.scalar(
        select(Integration).where(Integration.user_id == user.id, Integration.provider == "jira")
    )
    if integration:
        await session.delete(integration)
        await session.commit()
    return {"status": "disconnected"}