import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from supportops.jwt_inspection import UNVERIFIED_NOTICE, describe_duration, inspect_token
from supportops.targets import ORDERFLOW, JwtFormat

assert ORDERFLOW.jwt is not None
EXPECTED: JwtFormat = ORDERFLOW.jwt
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
SIGNATURE = "c2lnbmF0dXJlLW5vdC1yZWFsLXRlc3Qtb25seQ"


def segment(data: Any) -> str:
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def claims(**overrides: Any) -> dict[str, Any]:
    issued = int((NOW - timedelta(minutes=5)).timestamp())
    values: dict[str, Any] = {
        "iss": "orderflow",
        "sub": "5",
        "iat": issued,
        "exp": issued + 1800,
        "role": "CUSTOMER",
    }
    values.update(overrides)
    return {name: value for name, value in values.items() if value is not None}


def token(payload: Any = None, header: Any = None, signature: str = SIGNATURE) -> str:
    return ".".join(
        [
            segment(header or {"alg": "HS256"}),
            segment(claims() if payload is None else payload),
            signature,
        ]
    )


def inspect(raw: str | None) -> Any:
    return inspect_token(raw, "environment variable ORDERFLOW_TOKEN", EXPECTED, NOW)


def all_text(result: Any) -> str:
    return " ".join(
        [*result.problems, *result.claim_problems, result.model_dump_json(), repr(result)]
    )


def assert_no_token_material(raw: str, text: str) -> None:
    assert raw.strip() not in text
    for part in raw.strip().split("."):
        if len(part) > 8:
            assert part not in text


def test_a_valid_token_is_decoded_but_never_marked_verified() -> None:
    raw = token()

    result = inspect(raw)

    assert result.structure == "jwt"
    assert result.verified is False
    assert result.notice == UNVERIFIED_NOTICE
    assert "unverified and untrusted" in result.notice
    assert result.problems == []
    assert result.claim_problems == []
    assert result.claims is not None
    assert (result.claims.subject, result.claims.role, result.claims.issuer) == (
        "5",
        "CUSTOMER",
        "orderflow",
    )
    assert result.claims.algorithm == "HS256"
    assert result.claims.seconds_until_expiry == 25 * 60
    assert result.sendable
    assert_no_token_material(raw, all_text(result))


def test_an_expired_token_is_reported_as_a_claim() -> None:
    issued = int((NOW - timedelta(hours=2)).timestamp())

    result = inspect(token(claims(iat=issued, exp=issued + 1800)))

    assert result.expired
    assert result.claims.expired_beyond_skew
    assert result.claim_problems == [
        "The token claims it expired at 2026-10-09T10:30:00Z, 1 h 30 min ago."
    ]


def test_an_expiry_inside_the_clock_skew_is_flagged_as_such() -> None:
    expiry = int((NOW - timedelta(seconds=20)).timestamp())

    result = inspect(token(claims(exp=expiry)))

    assert result.expired
    assert not result.claims.expired_beyond_skew
    assert "60-second clock-skew allowance" in result.claim_problems[0]


def test_a_future_issued_at_is_a_clock_difference_not_a_rejection_cause() -> None:
    issued = int((NOW + timedelta(minutes=10)).timestamp())

    result = inspect(token(claims(iat=issued, exp=issued + 1800)))

    [problem] = result.claim_problems
    assert "10 min in the future" in problem
    assert "doesn't reject tokens for this" in problem


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (claims(role=None), "no role claim(s)"),
        (claims(iss=None, sub=None), "no iss, sub claim(s)"),
        (claims(iss="orderflow-dev"), "claims issuer 'orderflow-dev'"),
        (claims(role="SUPERUSER"), "claims role 'SUPERUSER'"),
        (claims(exp="tomorrow"), "exp claim isn't a number of seconds"),
        (claims(iat=True), "iat claim isn't a number of seconds"),
    ],
)
def test_claim_problems(payload: dict[str, Any], expected: str) -> None:
    result = inspect(token(payload))

    assert result.structure == "jwt"
    assert any(expected in problem for problem in result.claim_problems), result.claim_problems


def test_the_flags_behind_the_claim_problems() -> None:
    assert inspect(token(claims(iss="other"))).claims.unexpected_issuer
    assert inspect(token(claims(role="GUEST"))).claims.unknown_role


@pytest.mark.parametrize("algorithm", ["none", "RS256", None])
def test_an_unexpected_algorithm_is_reported(algorithm: str | None) -> None:
    header = {"alg": algorithm} if algorithm else {"typ": "JWT"}

    result = inspect(token(header=header))

    assert any("only accepts HS256" in problem for problem in result.claim_problems)


def test_an_unsigned_token_is_reported() -> None:
    result = inspect(token(header={"alg": "none"}, signature=""))

    assert "no signature part" in " ".join(result.problems)


@pytest.mark.parametrize(
    ("raw", "structure", "expected"),
    [
        ("not-a-token", "not_jwt", "this has 1"),
        ("a.b", "not_jwt", "this has 2"),
        ("bk_juniper01_lab_only_not_a_real_key", "not_jwt", "billing lab API key"),
        ("@@@.e30.sig", "malformed", "header isn't valid base64url"),
        (f"{segment({'alg': 'HS256'})}.%%%.sig", "malformed", "payload isn't valid base64url"),
        (f"{segment({'alg': 'HS256'})}.{segment(b'not json')}.sig", "malformed", "payload isn't"),
        (f"{segment({'alg': 'HS256'})}.{segment([1, 2])}.sig", "malformed", "not an object"),
    ],
)
def test_values_that_are_not_decodable_tokens(raw: str, structure: str, expected: str) -> None:
    result = inspect(raw)

    assert result.structure == structure
    assert result.claims is None
    assert any(expected in problem for problem in result.problems), result.problems
    assert_no_token_material(raw, all_text(result))


@pytest.mark.parametrize(
    ("wrap", "expected", "sendable"),
    [
        (lambda value: f"Bearer {value}", "starts with 'Bearer '", False),
        (lambda value: f'"{value}"', "wrapped in quotes", True),
        (lambda value: f"{value}\n", "line break", False),
        (lambda value: f" {value}", "start or end", False),
    ],
)
def test_string_problems_are_found_and_the_token_is_still_decoded(
    wrap: Any, expected: str, sendable: bool
) -> None:
    raw = wrap(token())

    result = inspect(raw)

    assert any(expected in problem for problem in result.problems), result.problems
    assert result.structure == "jwt"
    assert result.sendable is sendable
    assert_no_token_material(token(), all_text(result))


def test_a_missing_token() -> None:
    result = inspect(None)

    assert (result.present, result.structure) == (False, "missing")
    assert result.problems == ["No token is set in environment variable ORDERFLOW_TOKEN."]


def test_unprintable_or_huge_claim_values_are_not_shown_in_full() -> None:
    result = inspect(token(claims(sub="x" * 500, role="CUSTOMER\u0000")))

    assert result.claims.subject == "x" * 80 + "..."
    assert result.claims.role is None


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (45, "45 s"),
        (-600, "10 min"),
        (3 * 3600 + 120, "3 h 2 min"),
        (7200, "2 h"),
        (5 * 86400, "5 days"),
    ],
)
def test_durations(seconds: int, text: str) -> None:
    assert describe_duration(seconds) == text
