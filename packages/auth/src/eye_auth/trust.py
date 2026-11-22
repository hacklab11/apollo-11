"""JWT access-token and A2A card verification against one pinned issuer."""

import asyncio
import os
from contextvars import ContextVar
from functools import lru_cache
from typing import Any

import jwt
from a2a.types import AgentCard
from a2a.utils.signing import SignatureVerificationError, create_signature_verifier
from fastapi import HTTPException, Request
from fastapi.security import HTTPBearer
from jwt import PyJWKClient
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

_bearer = HTTPBearer(auto_error=False)
_scopes: ContextVar[frozenset[str]] = ContextVar("eye_auth_scopes", default=frozenset())


def granted_scopes() -> frozenset[str]:
    return _scopes.get()


def check_skill(agent: str, skill: str, scopes: set[str] | frozenset[str]) -> None:
    if f"{agent}:{skill}" not in scopes:
        raise PermissionError(f"Insufficient scope for {agent}:{skill}")


class SkillGuard:
    def __init__(self, agent: str, scopes: frozenset[str]) -> None:
        self.agent = agent
        self.scopes = scopes
        self.denied = False

    def check(self, skill: str) -> None:
        try:
            check_skill(self.agent, skill, self.scopes)
        except PermissionError:
            self.denied = True
            raise

    def finish(self) -> None:
        if self.denied:
            raise PermissionError("A requested skill was not authorized")


def issuer() -> str:
    return os.environ.get("EYE_AUTH_URL", "http://127.0.0.1:8500").rstrip("/")


@lru_cache
def jwks(url: str) -> PyJWKClient:
    return PyJWKClient(url + "/.well-known/jwks.json", timeout=5, cooldown_duration=0)


def decode_token(token: str, audience: str) -> dict[str, Any]:
    keys = jwks(issuer())
    key = keys.get_signing_key_from_jwt(token)
    if jwt.get_unverified_header(token).get("typ") != "at+jwt":
        raise jwt.InvalidTokenError("not an access token")
    return jwt.decode(
        token,
        key.key,
        algorithms=["RS256"],
        issuer=issuer(),
        audience=audience,
        options={"require": ["iss", "aud", "exp", "iat", "sub", "jti", "scope"]},
    )


async def require_scopes(request: Request, audience: str, scopes: set[str]) -> dict:
    credentials = await _bearer(request)
    try:
        if credentials is None:
            raise jwt.InvalidTokenError("missing token")
        claims = await asyncio.to_thread(
            decode_token, credentials.credentials, audience
        )
    except jwt.PyJWTError:
        raise HTTPException(
            401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    if not scopes.issubset(claims["scope"].split()):
        raise HTTPException(403, "Insufficient scope")
    return claims


async def verify_card(card: AgentCard, expected_name: str | None = None) -> None:
    def verify() -> None:
        def key_provider(kid: str | None, _jku: str | None) -> jwt.PyJWK:
            # Never follow the card's jku: only the configured issuer is trusted.
            return jwks(issuer()).get_signing_key(kid)

        try:
            create_signature_verifier(key_provider, ["RS256"])(card)
        except (SignatureVerificationError, jwt.PyJWTError) as exc:
            raise ValueError("Untrusted agent card") from exc
        if expected_name is not None and card.name != expected_name:
            raise ValueError("Agent identity mismatch")

    await asyncio.to_thread(verify)


class A2AAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, services: dict[str, tuple[str, set[str]]]) -> None:
        super().__init__(app)
        self.services = services

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        service = self.services.get(request.url.path)
        if service is not None:
            audience, scopes = service
            try:
                claims = await require_scopes(request, audience, scopes)
            except HTTPException as exc:
                return JSONResponse(
                    {"detail": exc.detail}, exc.status_code, headers=exc.headers
                )
            token = _scopes.set(frozenset(claims["scope"].split()))
            try:
                return await call_next(request)
            finally:
                _scopes.reset(token)
        return await call_next(request)
