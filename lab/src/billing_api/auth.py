import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader

from billing_api import repository
from billing_api.dependencies import DbConnection
from billing_api.errors import ApiError
from billing_api.repository import ApiKeyRecord

KEY_PATTERN = re.compile(r"bk_[A-Za-z0-9_]{17,125}")
KEY_PREFIX_LENGTH = 12
UNAUTHENTICATED_HEADERS = {"WWW-Authenticate": 'Bearer realm="billing-api"'}

logger = logging.getLogger("billing_api.auth")

_authorization_header = APIKeyHeader(
    name="Authorization",
    scheme_name="ApiKey",
    description="Send your API key as `Bearer <api key>`, for example `Bearer bk_...`.",
    auto_error=False,
)


@dataclass(frozen=True)
class AuthenticatedAccount:
    account_id: str
    account_name: str
    key_prefix: str
    key_label: str
    key_created_at: datetime


class AuthenticationFailed(Exception):
    def __init__(
        self, reason: str, *, key_prefix: str | None = None, account_id: str | None = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.key_prefix = key_prefix
        self.account_id = account_id


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def parse_api_key(authorization: str | None) -> str:
    if authorization is None or not authorization.strip():
        raise AuthenticationFailed("missing_header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not KEY_PATTERN.fullmatch(token):
        raise AuthenticationFailed("malformed_header")
    return token


def verify_api_key(api_key: str, record: ApiKeyRecord | None) -> AuthenticatedAccount:
    prefix = api_key[:KEY_PREFIX_LENGTH]
    if record is None or not hmac.compare_digest(record.key_hash, hash_api_key(api_key)):
        raise AuthenticationFailed("unknown_key", key_prefix=prefix)
    if record.revoked:
        raise AuthenticationFailed("revoked_key", key_prefix=prefix, account_id=record.account_id)
    if record.expired:
        raise AuthenticationFailed("expired_key", key_prefix=prefix, account_id=record.account_id)
    if record.account_status != "active":
        raise AuthenticationFailed(
            "account_suspended", key_prefix=prefix, account_id=record.account_id
        )
    return AuthenticatedAccount(
        account_id=record.account_id,
        account_name=record.account_name,
        key_prefix=prefix,
        key_label=record.label,
        key_created_at=record.created_at,
    )


def authenticate(
    request: Request,
    connection: DbConnection,
    authorization: Annotated[str | None, Depends(_authorization_header)],
) -> AuthenticatedAccount:
    try:
        api_key = parse_api_key(authorization)
        account = verify_api_key(
            api_key, repository.find_api_key(connection, api_key[:KEY_PREFIX_LENGTH])
        )
    except AuthenticationFailed as failure:
        _log_rejection(failure)
        if failure.reason == "account_suspended":
            raise ApiError(
                403,
                "ACCOUNT_SUSPENDED",
                "This account is suspended. Contact support to restore access.",
            ) from None
        raise ApiError(
            401,
            "UNAUTHENTICATED",
            "Missing or invalid API key.",
            headers=UNAUTHENTICATED_HEADERS,
        ) from None
    request.state.account_id = account.account_id
    return account


def _log_rejection(failure: AuthenticationFailed) -> None:
    extra: dict[str, Any] = {"event_name": "auth.rejected", "reason": failure.reason}
    if failure.key_prefix is not None:
        extra["key_prefix"] = failure.key_prefix
    if failure.account_id is not None:
        extra["account_id"] = failure.account_id
    logger.warning("API key rejected", extra=extra)


CurrentAccount = Annotated[AuthenticatedAccount, Depends(authenticate)]
