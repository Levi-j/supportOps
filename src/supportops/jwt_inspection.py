import base64
import binascii
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from supportops.targets import JwtFormat

UNVERIFIED_NOTICE = (
    "Claims were decoded locally without checking the signature. They are unverified and "
    "untrusted: only the service can tell whether the token is genuine."
)
MAX_CLAIM_CHARACTERS = 80
_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
_BEARER_PREFIX = re.compile(r"(?i)^bearer\s+")

TokenStructure = Literal["missing", "not_jwt", "malformed", "jwt"]


class TokenClaims(BaseModel):
    algorithm: str | None = None
    token_type: str | None = None
    subject: str | None = None
    issuer: str | None = None
    role: str | None = None
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    not_before: datetime | None = None
    seconds_until_expiry: int | None = None
    expired_beyond_skew: bool = False
    unexpected_issuer: bool = False
    unknown_role: bool = False


class TokenInspection(BaseModel):
    source: str
    present: bool
    structure: TokenStructure
    sendable: bool = False
    problems: list[str] = Field(default_factory=list)
    claims: TokenClaims | None = None
    claim_problems: list[str] = Field(default_factory=list)
    verified: Literal[False] = False
    notice: str = UNVERIFIED_NOTICE

    @property
    def expired(self) -> bool:
        seconds = self.claims.seconds_until_expiry if self.claims else None
        return seconds is not None and seconds <= 0


def inspect_token(
    raw: str | None, source: str, expected: JwtFormat, now: datetime
) -> TokenInspection:
    if not raw:
        return TokenInspection(
            source=source,
            present=False,
            structure="missing",
            problems=[f"No token is set in {source}."],
        )
    problems = _string_problems(raw)
    core = _BEARER_PREFIX.sub("", raw.strip()).strip("\"'")
    sendable = not re.search(r"\s", raw)
    parts = core.split(".")
    if len(parts) != 3:
        problems.append(_not_a_jwt(core, len(parts)))
        return TokenInspection(
            source=source, present=True, structure="not_jwt", sendable=sendable, problems=problems
        )
    header, header_problem = _decode(parts[0], "header")
    payload, payload_problem = _decode(parts[1], "payload")
    for problem in (header_problem, payload_problem):
        if problem:
            problems.append(problem)
    if header is None or payload is None:
        return TokenInspection(
            source=source, present=True, structure="malformed", sendable=sendable, problems=problems
        )
    if not parts[2]:
        problems.append("The token has no signature part, so the service would reject it.")
    claims, claim_problems = _claims(header, payload, expected, now)
    return TokenInspection(
        source=source,
        present=True,
        structure="jwt",
        sendable=sendable,
        problems=problems,
        claims=claims,
        claim_problems=claim_problems,
    )


def describe_duration(seconds: int) -> str:
    seconds = abs(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    return f"{hours // 24} days"


def format_instant(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _string_problems(raw: str) -> list[str]:
    problems = []
    stripped = raw.strip()
    if stripped != raw:
        problems.append(
            "The token has spaces or line breaks at the start or end, which often come along "
            "when it is copied from a terminal or a document."
        )
    if "\n" in raw or "\r" in raw:
        problems.append("The token contains a line break. Tokens are always a single line.")
    if _BEARER_PREFIX.match(stripped):
        problems.append(
            "The value starts with 'Bearer '. Store only the token; SupportOps adds the "
            "'Bearer' scheme itself."
        )
    unprefixed = _BEARER_PREFIX.sub("", stripped)
    if unprefixed.strip("\"'") != unprefixed:
        problems.append("The token is wrapped in quotes. Remove them.")
    if re.search(r"\s", unprefixed):
        problems.append("The token contains spaces. Tokens never contain whitespace.")
    return problems


def _not_a_jwt(core: str, parts: int) -> str:
    if core.startswith("bk_"):
        return (
            "This looks like a billing lab API key, not a token. This target expects a bearer "
            "token from its login endpoint."
        )
    return f"The value isn't a JWT: a JWT has three parts separated by dots, but this has {parts}."


def _decode(segment: str, name: str) -> tuple[dict[str, Any] | None, str | None]:
    invalid = f"The token's {name} isn't valid base64url-encoded JSON."
    if not _SEGMENT.fullmatch(segment):
        return None, invalid
    try:
        data = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (binascii.Error, ValueError):
        return None, invalid
    if not isinstance(data, dict):
        return None, f"The token's {name} is JSON, but not an object."
    return data, None


def _claims(
    header: dict[str, Any], payload: dict[str, Any], expected: JwtFormat, now: datetime
) -> tuple[TokenClaims, list[str]]:
    problems: list[str] = []
    issued_at = _time(payload, "iat", problems)
    expires_at = _time(payload, "exp", problems)
    not_before = _time(payload, "nbf", problems)
    claims = TokenClaims(
        algorithm=_text(header.get("alg")),
        token_type=_text(header.get("typ")),
        subject=_text(payload.get("sub")),
        issuer=_text(payload.get("iss")),
        role=_text(payload.get(expected.role_claim)),
        issued_at=issued_at,
        expires_at=expires_at,
        not_before=not_before,
        seconds_until_expiry=int((expires_at - now).total_seconds()) if expires_at else None,
    )
    missing = [name for name in expected.required_claims if name not in payload]
    if missing:
        problems.append(
            f"The token has no {', '.join(missing)} claim(s), which the service expects."
        )
    if claims.algorithm != expected.algorithm:
        shown = claims.algorithm or "no algorithm"
        problems.append(
            f"The token's header names {shown}; the service only accepts {expected.algorithm}."
        )
    if claims.issuer is not None and claims.issuer != expected.issuer:
        claims.unexpected_issuer = True
        problems.append(
            f"The token claims issuer '{claims.issuer}', but the service only accepts "
            f"'{expected.issuer}'."
        )
    if claims.role is not None and claims.role not in expected.roles:
        claims.unknown_role = True
        problems.append(
            f"The token claims role '{claims.role}', which the service doesn't recognise, so it "
            "would grant no role."
        )
    skew = expected.clock_skew_seconds
    if expires_at is not None and claims.seconds_until_expiry is not None:
        remaining = claims.seconds_until_expiry
        claims.expired_beyond_skew = -remaining >= skew
        if remaining <= 0:
            allowance = (
                f" That is within the service's {skew}-second clock-skew allowance, so it may "
                "still be accepted briefly."
                if -remaining < skew
                else ""
            )
            problems.append(
                f"The token claims it expired at {format_instant(expires_at)}, "
                f"{describe_duration(remaining)} ago.{allowance}"
            )
    if not_before is not None and (not_before - now).total_seconds() > skew:
        problems.append(
            f"The token claims it isn't valid before {format_instant(not_before)}, which is "
            "still in the future."
        )
    if issued_at is not None and (issued_at - now).total_seconds() > skew:
        problems.append(
            f"The token claims it was issued at {format_instant(issued_at)}, "
            f"{describe_duration(int((issued_at - now).total_seconds()))} in the future. That "
            "points to clocks that differ between machines; the service doesn't reject tokens "
            "for this."
        )
    return claims, problems


def _time(payload: dict[str, Any], name: str, problems: list[str]) -> datetime | None:
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        problems.append(f"The token's {name} claim isn't a number of seconds.")
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        problems.append(f"The token's {name} claim is outside the range of valid times.")
        return None


def _text(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not value or not value.isprintable():
        return None
    if len(value) > MAX_CLAIM_CHARACTERS:
        return value[:MAX_CLAIM_CHARACTERS] + "..."
    return value
