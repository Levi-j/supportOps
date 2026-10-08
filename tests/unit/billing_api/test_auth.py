from dataclasses import replace
from datetime import UTC, datetime

import pytest

from billing_api.auth import (
    AuthenticationFailed,
    hash_api_key,
    parse_api_key,
    verify_api_key,
)
from billing_api.repository import ApiKeyRecord

API_KEY = "bk_juniper01_lab_only_not_a_real_key"

VALID_RECORD = ApiKeyRecord(
    key_id="key_juniper_main",
    account_id="acct_juniper",
    account_name="Juniper Dental Group",
    account_status="active",
    key_hash=hash_api_key(API_KEY),
    label="Practice software",
    created_at=datetime(2026, 1, 1, tzinfo=UTC),
    revoked=False,
    expired=False,
)


def test_hash_is_sha256_hex() -> None:
    assert hash_api_key("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_parses_a_bearer_key() -> None:
    assert parse_api_key(f"Bearer {API_KEY}") == API_KEY
    assert parse_api_key(f"bearer {API_KEY}") == API_KEY


@pytest.mark.parametrize("header", [None, "", "   "])
def test_missing_header(header: str | None) -> None:
    with pytest.raises(AuthenticationFailed) as excinfo:
        parse_api_key(header)

    assert excinfo.value.reason == "missing_header"
    assert excinfo.value.key_prefix is None


@pytest.mark.parametrize(
    "header",
    [
        f"Basic {API_KEY}",
        "Bearer",
        "Bearer ",
        f"Bearer  {API_KEY}",
        f"Bearer {API_KEY} extra",
        "Bearer bk_short",
        "Bearer sk_test_not_ours_0000000000",
        f'Bearer "{API_KEY}"',
    ],
)
def test_malformed_header(header: str) -> None:
    with pytest.raises(AuthenticationFailed) as excinfo:
        parse_api_key(header)

    assert excinfo.value.reason == "malformed_header"
    assert excinfo.value.key_prefix is None


def test_valid_key_returns_the_account() -> None:
    account = verify_api_key(API_KEY, VALID_RECORD)

    assert account.account_id == "acct_juniper"
    assert account.key_prefix == "bk_juniper01"
    assert account.key_label == "Practice software"


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (None, "unknown_key"),
        (
            replace(VALID_RECORD, key_hash=hash_api_key("bk_juniper01_a_different_secret")),
            "unknown_key",
        ),
        (replace(VALID_RECORD, revoked=True), "revoked_key"),
        (replace(VALID_RECORD, expired=True), "expired_key"),
        (replace(VALID_RECORD, account_status="suspended"), "account_suspended"),
    ],
)
def test_rejections_carry_an_internal_reason_and_only_the_prefix(
    record: ApiKeyRecord | None, reason: str
) -> None:
    with pytest.raises(AuthenticationFailed) as excinfo:
        verify_api_key(API_KEY, record)

    assert excinfo.value.reason == reason
    assert excinfo.value.key_prefix == "bk_juniper01"


def test_revoked_wins_over_suspended() -> None:
    record = replace(VALID_RECORD, revoked=True, account_status="suspended")

    with pytest.raises(AuthenticationFailed) as excinfo:
        verify_api_key(API_KEY, record)

    assert excinfo.value.reason == "revoked_key"
