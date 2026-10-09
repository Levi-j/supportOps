import re

import pytest

from supportops.db.catalog import CATALOG, CHECKS, Parameter, get_check, placeholders
from supportops.errors import ConfigError

FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|truncate|drop|alter|create|grant|revoke|copy|vacuum|lock|"
    r"set|call|do|pg_terminate_backend|pg_cancel_backend|pg_reload_conf|nextval|setval)\b",
    re.IGNORECASE,
)


def test_check_names_are_unique_and_namespaced() -> None:
    names = [check.name for check in CATALOG]

    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"(db|pg|billing)\.[a-z_]+", name) for name in names)
    assert set(CHECKS) == set(names)


def test_every_required_check_is_in_the_catalog() -> None:
    assert set(CHECKS) == {
        "db.connectivity",
        "pg.connections",
        "pg.long_transactions",
        "pg.blocking_sessions",
        "billing.invoice_total_mismatch",
        "billing.paid_invoice_without_payment",
        "billing.payment_on_unpaid_invoice",
        "billing.duplicate_payments",
        "billing.api_key_status",
        "billing.invoice_lookup",
    }


@pytest.mark.parametrize("check", CATALOG, ids=lambda check: check.name)
def test_metadata_is_complete(check: object) -> None:
    from supportops.db.catalog import Check

    assert isinstance(check, Check)
    assert check.description
    assert check.pack in ("generic", "billing")
    if check.kind in ("consistency", "activity", "lookup"):
        assert check.ok_message
        assert check.problem_message


@pytest.mark.parametrize("check", CATALOG, ids=lambda check: check.name)
def test_sql_is_a_single_read_only_select(check: object) -> None:
    from supportops.db.catalog import Check

    assert isinstance(check, Check)
    sql = check.sql.strip()
    assert sql.upper().startswith("SELECT")
    assert ";" not in sql
    without_literals = re.sub(r"'[^']*'", "''", sql)
    assert not FORBIDDEN_SQL.search(without_literals), FORBIDDEN_SQL.search(without_literals)


@pytest.mark.parametrize("check", CATALOG, ids=lambda check: check.name)
def test_placeholders_match_declared_parameters(check: object) -> None:
    from supportops.db.catalog import Check

    assert isinstance(check, Check)
    assert placeholders(check.sql) == {parameter.name for parameter in check.parameters}
    assert "%" not in re.sub(r"%\(\w+\)s", "", check.sql)
    assert set(check.one_of) <= {parameter.name for parameter in check.parameters}


def test_no_check_reads_secrets_or_customer_contact_details() -> None:
    for check in CATALOG:
        assert "key_hash" not in check.sql
        assert "email" not in check.sql


def test_unknown_check_names_are_rejected() -> None:
    with pytest.raises(ConfigError, match=r"Unknown check 'billing\.drop_everything'"):
        get_check("billing.drop_everything")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", 0), ("60", 60), (" 300 ", 300), ("86400", 86_400)],
)
def test_integer_parameters(raw: str, expected: int) -> None:
    minimum = CHECKS["pg.long_transactions"].parameter("min_seconds")

    assert minimum is not None
    assert minimum.parse(raw) == expected


@pytest.mark.parametrize("raw", ["-1", "1.5", "ten", "86401", "1; DROP TABLE x"])
def test_invalid_integer_parameters(raw: str) -> None:
    minimum = CHECKS["pg.long_transactions"].parameter("min_seconds")

    assert minimum is not None
    with pytest.raises(ConfigError, match="min_seconds"):
        minimum.parse(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "bk_juniper01_lab_only_not_a_real_key",
        "bk_short",
        "bk_juniper01' OR '1'='1",
        "",
    ],
)
def test_prefix_must_be_exactly_twelve_safe_characters(raw: str) -> None:
    prefix = CHECKS["billing.api_key_status"].parameter("prefix")

    assert prefix is not None
    with pytest.raises(ConfigError) as excinfo:
        prefix.parse(raw)

    assert raw not in excinfo.value.message or raw == ""
    assert excinfo.value.hint is not None
    assert "bk_juniper01" in excinfo.value.hint


def test_text_parameters_accept_valid_values() -> None:
    lookup = CHECKS["billing.invoice_lookup"]
    invoice_id = lookup.parameter("id")
    number = lookup.parameter("number")

    assert invoice_id is not None
    assert number is not None
    assert invoice_id.parse("inv_juniper_1003") == "inv_juniper_1003"
    assert number.parse("INV-1003") == "INV-1003"


def test_usage_explains_required_and_alternative_parameters() -> None:
    assert CHECKS["billing.api_key_status"].usage() == (
        "Needs --param prefix (for example prefix=bk_juniper01)."
    )
    assert CHECKS["billing.invoice_lookup"].usage() == (
        "Needs --param id or --param number (for example id=inv_juniper_1003 or number=INV-1003)."
    )


def test_parameter_defaults() -> None:
    assert Parameter(name="x", description="X").default is None
    minimum = CHECKS["pg.long_transactions"].parameter("min_seconds")
    assert minimum is not None
    assert minimum.default == 60
