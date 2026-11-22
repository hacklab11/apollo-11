"""Authlib OAuth2 client-credentials issuer and agent-card signing authority."""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from secrets import compare_digest
from typing import Any

import jwt
import uvicorn
import yaml
from a2a.types import AgentCard
from a2a.utils.signing import create_agent_card_signer
from a2wsgi import WSGIMiddleware
from authlib.integrations.flask_oauth2 import AuthorizationServer
from authlib.oauth2.rfc6749 import ClientMixin, InvalidScopeError
from authlib.oauth2.rfc6749.grants import ClientCredentialsGrant
from authlib.oauth2.rfc9068 import JWTBearerTokenGenerator
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask, jsonify, request
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError

from eye_auth.trust import issuer


def audience(scope: str) -> str:
    return "eye-core" if scope == "selleros:revert" else scope.split(":", 1)[0]


def load_key(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        data = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with path.open("xb") as handle:
            path.chmod(0o600)
            handle.write(data)
    return path.read_bytes()


@dataclass
class Client(ClientMixin):
    client_id: str
    secret: str
    scopes: set[str]

    def get_client_id(self) -> str:
        return self.client_id

    def check_client_secret(self, client_secret: str) -> bool:
        return bool(self.secret) and compare_digest(
            self.secret.encode(), client_secret.encode()
        )

    def check_endpoint_auth_method(self, method: str, endpoint: str) -> bool:
        return endpoint == "token" and method == "client_secret_basic"

    def check_grant_type(self, grant_type: str) -> bool:
        return grant_type == "client_credentials"

    def get_allowed_scope(self, scope: str) -> str:
        requested = set((scope or "").split())
        if not requested or not requested.issubset(self.scopes):
            raise InvalidScopeError()
        if len({audience(s) for s in requested}) != 1:
            raise InvalidScopeError()
        return " ".join(sorted(requested))


def create_app(
    *, config_path: Path | None = None, key_path: Path | None = None
) -> Flask:
    config = yaml.safe_load(
        (
            config_path or Path(os.environ.get("EYE_AUTH_CONFIG", "config/auth.yaml"))
        ).read_text()
    )
    clients = {
        name: Client(name, os.environ.get(item["secret_env"], ""), set(item["scopes"]))
        for name, item in config["clients"].items()
    }
    private = load_key(
        key_path or Path(os.environ.get("EYE_AUTH_KEY_PATH", "data/auth/key.pem"))
    )
    key = serialization.load_pem_private_key(private, password=None)
    public = key.public_key()
    kid = hashlib.sha256(
        public.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).hexdigest()[:24]
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public)) | {
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
    }
    private_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key)) | {
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
    }

    class Generator(JWTBearerTokenGenerator):
        def get_jwks(self) -> dict:
            return {"keys": [private_jwk]}

        def get_audiences(self, client: Client, user: Any, scope: str) -> str:
            return audience(scope.split()[0])

    class Grant(ClientCredentialsGrant):
        def validate_token_request(self) -> None:
            super().validate_token_request()
            self.request.client.get_allowed_scope(self.request.payload.scope)

    app = Flask(__name__)
    # RFC 6749 transport security is terminated at deployment ingress; the
    # isolated local Compose network uses HTTP for the offline gate.
    server = AuthorizationServer(
        app, query_client=clients.get, save_token=lambda token, request: None
    )
    server.register_grant(Grant)
    server.register_token_generator(
        "default", Generator(issuer(), expires_generator=300)
    )

    @app.post("/oauth/token")
    def token() -> Any:
        return server.create_token_response()

    @app.get("/.well-known/jwks.json")
    def keys() -> Any:
        return jsonify({"keys": [public_jwk]})

    @app.get("/healthz")
    def health() -> Any:
        return jsonify({"status": "ok"})

    @app.post("/cards/sign")
    def sign() -> Any:
        auth = request.authorization
        try:
            if auth is None or auth.type.lower() != "bearer":
                raise jwt.InvalidTokenError()
            if jwt.get_unverified_header(auth.token).get("typ") != "at+jwt":
                raise jwt.InvalidTokenError()
            claims = jwt.decode(
                auth.token,
                public,
                algorithms=["RS256"],
                audience="auth",
                issuer=issuer(),
                options={"require": ["exp", "sub", "scope", "jti", "iat"]},
            )
        except jwt.PyJWTError:
            return jsonify({"error": "invalid_token"}), 401
        if "auth:card" not in claims["scope"].split():
            return jsonify({"error": "insufficient_scope"}), 403
        try:
            card = ParseDict(request.get_json(), AgentCard())
        except (ParseError, TypeError):
            return jsonify({"error": "invalid_card"}), 400
        if card.name != claims["sub"] or card.name not in config["card_clients"]:
            return jsonify({"error": "identity_mismatch"}), 403
        del card.signatures[:]
        create_agent_card_signer(
            private,
            {
                "kid": kid,
                "alg": "RS256",
                "jku": issuer() + "/.well-known/jwks.json",
                "typ": "JOSE",
            },
        )(card)
        return jsonify(MessageToDict(card))

    return app


def main() -> None:
    uvicorn.run(
        WSGIMiddleware(create_app()),
        host=os.environ.get("EYE_BIND_HOST", "127.0.0.1"),
        port=8500,
        access_log=False,
    )
