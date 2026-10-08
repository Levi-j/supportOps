import pytest

from supportops.redaction import mask_api_key, redact_dsn, redact_text

REDACTED_CASES = [
    ("Authorization: Bearer abcdefghijklmnop", "Authorization: Bearer ***"),
    ("authorization=Basic dXNlcjpwYXNzd29yZA==", "authorization=Basic ***"),
    ("Authorization: rawtokenvalue123", "Authorization: ***"),
    ("Bearer bk_labjunip1a2b3c4d5e6f7g8h9", "Bearer bk_labjunip1***"),
    ("key bk_labjunip1a2b3c4d5e6f7g8h9 was rejected", "key bk_labjunip1*** was rejected"),
    ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI0MiJ9.c2lnbmF0dXJl here", "token *** here"),
    (
        "connect postgresql://app:hunter2@db:5432/billing failed",
        "connect postgresql://app:***@db:5432/billing failed",
    ),
    ("POSTGRES_PASSWORD=hunter2", "POSTGRES_PASSWORD=***"),
    ("password: hunter2", "password: ***"),
    ("client_secret=abc123", "client_secret=***"),
    ('{"password": "hunter2", "user": "ann"}', '{"password": "***", "user": "ann"}'),
    (
        "contact ann.lee@juniper-dental.example today",
        "contact a***@juniper-dental.example today",
    ),
]

UNCHANGED_CASES = [
    'password authentication failed for user "billing_app"',
    "request_id=inc001-cust-01 status=401 duration_ms=4",
    "http://localhost:8000/v1/invoices?limit=3",
    "postgresql://supportops_ro@localhost:5433/billing",
    "bk_wrong",
    "Bearer token expired",
    "Secret scanning: enabled",
]


@pytest.mark.parametrize(("text", "expected"), REDACTED_CASES)
def test_redact_text_masks_secrets(text: str, expected: str) -> None:
    assert redact_text(text) == expected


@pytest.mark.parametrize(("text", "expected"), REDACTED_CASES)
def test_redact_text_is_idempotent(text: str, expected: str) -> None:
    assert redact_text(expected) == expected


@pytest.mark.parametrize("text", UNCHANGED_CASES)
def test_redact_text_leaves_ordinary_text_alone(text: str) -> None:
    assert redact_text(text) == text


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (
            "postgresql://supportops_ro:s3cr3t@localhost:5433/billing",
            "postgresql://supportops_ro:***@localhost:5433/billing",
        ),
        (
            "postgresql://app:p@ss/w0rd@db.internal:5432/billing",
            "postgresql://app:***@db.internal:5432/billing",
        ),
        (
            "postgresql://supportops_ro@localhost:5433/billing",
            "postgresql://supportops_ro@localhost:5433/billing",
        ),
        ("postgresql://localhost/billing", "postgresql://localhost/billing"),
        ("not a url", "not a url"),
    ],
)
def test_redact_dsn(dsn: str, expected: str) -> None:
    assert redact_dsn(dsn) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("bk_labjunip1a2b3c4d5e6f7g8h9", "bk_labjunip1***"),
        ("bk_labjunip1***", "bk_labjunip1***"),
        ("bk_short", "***"),
        ("not-a-billing-key-123456", "***"),
    ],
)
def test_mask_api_key_keeps_only_the_prefix(value: str, expected: str) -> None:
    assert mask_api_key(value) == expected
