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
    assert all(re.fullmatch(r"(db|pg|billing|orderflow)\.[a-z_]+", name) for name in names)
    assert all(
        check.name.split(".")[0] == check.pack
        for check in CATALOG
        if check.pack in ("billing", "orderflow")
    )
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
        "orderflow.inventory_mismatch",
        "orderflow.order_total_mismatch",
        "orderflow.orders_without_items",
        "orderflow.order_lookup",
    }


@pytest.mark.parametrize("check", CATALOG, ids=lambda check: check.name)
def test_metadata_is_complete(check: object) -> None:
    from supportops.db.catalog import Check

    assert isinstance(check, Check)
    assert check.description
    assert check.pack in ("generic", "billing", "orderflow")
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
        assert "password_hash" not in check.sql
        assert "request_hash" not in check.sql
        assert ".note" not in check.sql
        assert "public.users" not in check.sql


def test_orderflow_checks_never_return_the_idempotency_key_itself() -> None:
    for check in CATALOG:
        if check.pack != "orderflow":
            continue
        mentions = re.findall(r"[\w.]*idempotency_key[^,\n]*", check.sql)
        assert all(mention.endswith("IS NOT NULL AS has_idempotency_key") for mention in mentions)
        assert "public." in check.sql


def test_the_order_lookup_takes_a_positive_integer_id() -> None:
    order_id = CHECKS["orderflow.order_lookup"].parameter("id")

    assert order_id is not None
    assert order_id.required
    assert order_id.parse("42") == 42
    for raw in ("0", "-1", "42; DROP TABLE orders", "abc"):
        with pytest.raises(ConfigError):
            order_id.parse(raw)
    assert CHECKS["orderflow.order_lookup"].usage() == ("Needs --param id (for example id=42).")


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
