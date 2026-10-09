import ast
import json
import re
from pathlib import Path

import pytest

from billing_api.auth import KEY_PATTERN
from billing_api.faults import Fault
from supportops.errors import ConfigError
from supportops.http_checks import REQUEST_ID_PATTERN, WRITE_METHODS
from supportops.investigation.rules import RULES
from supportops_lab.scenarios import (
    LAB_KEYS,
    POWERSHELL_STRIPPED_BODY,
    SCENARIOS,
    VALID_CUSTOMER_BODY,
    get_scenario,
)

SRC = Path(__file__).resolve().parents[3] / "src"


def test_ids_slugs_and_request_ids_are_unique_and_valid() -> None:
    ids = [scenario.id for scenario in SCENARIOS]
    slugs = [scenario.slug for scenario in SCENARIOS]
    request_ids = [request.request_id for scenario in SCENARIOS for request in scenario.requests]

    assert ids == ["INC-001", "INC-002", "INC-003", "INC-004", "INC-005"]
    assert len(set(slugs)) == len(slugs)
    assert len(set(request_ids)) == len(request_ids)
    assert all(re.fullmatch(r"INC-\d{3}", item) for item in ids)
    assert all(re.fullmatch(r"[a-z0-9-]+", item) for item in slugs)
    assert all(REQUEST_ID_PATTERN.fullmatch(item) for item in request_ids)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda item: item.id)
def test_each_scenario_is_consistent(scenario: object) -> None:
    from supportops_lab.scenarios import Scenario

    assert isinstance(scenario, Scenario)
    assert all(fault in {item.value for item in Fault} for fault in scenario.faults)
    assert scenario.expectation.request_id in {item.request_id for item in scenario.requests}
    assert scenario.expectation.confidence in {"confirmed", "likely", "possible"}
    rule_names = {rule.__name__ for rule in RULES}
    assert set(scenario.expectation.rules) <= rule_names | {
        "key_revoked",
        "payment_invoice_inconsistent",
        "api_cannot_reach_database",
    }
    for request in scenario.requests:
        assert request.api_key is None or request.api_key in LAB_KEYS
        assert request.method in {"GET", *WRITE_METHODS}
        assert request.path.startswith("/v1/")
        if request.expected_status == 503:
            assert request.expected_code in {"SERVICE_UNAVAILABLE", "DATABASE_BUSY"}


def test_lab_keys_have_the_billing_format_and_say_they_are_fake() -> None:
    for key in LAB_KEYS:
        assert KEY_PATTERN.fullmatch(key)
        assert key.endswith("_lab_only_not_a_real_key")


def test_the_powershell_body_is_invalid_and_the_fixed_body_is_valid() -> None:
    with pytest.raises(ValueError, match="double quotes"):
        json.loads(POWERSHELL_STRIPPED_BODY)
    assert json.loads(VALID_CUSTOMER_BODY) == {"name": "Acme", "email": "billing@acme.example"}


@pytest.mark.parametrize("text", ["INC-001", "inc-001", " revoked-api-key "])
def test_scenarios_are_found_by_id_or_slug(text: str) -> None:
    assert get_scenario(text).id == "INC-001"


def test_unknown_scenarios_list_the_choices() -> None:
    with pytest.raises(ConfigError) as excinfo:
        get_scenario("INC-009")

    assert "INC-001 (revoked-api-key)" in (excinfo.value.hint or "")
    assert "INC-005 (blocked-writes)" in (excinfo.value.hint or "")


def test_only_the_rotation_scenario_writes_sql_and_it_is_scoped() -> None:
    with_sql = [scenario for scenario in SCENARIOS if scenario.setup_sql]

    assert [scenario.id for scenario in with_sql] == ["INC-001"]
    sql = with_sql[0].setup_sql or ""
    assert "DELETE" not in sql.upper()
    assert "DROP" not in sql.upper()
    assert "WHERE key_prefix = 'bk_kestrel01'" in sql


def test_only_the_misconfiguration_scenario_changes_the_api_database_host() -> None:
    changed = [scenario for scenario in SCENARIOS if scenario.api_database_host is not None]

    assert [(scenario.id, scenario.api_database_host) for scenario in changed] == [
        ("INC-004", "localhost")
    ]
    assert changed[0].faults == ()
    assert changed[0].setup_sql is None


def test_the_lock_holder_only_locks_one_invoice_row() -> None:
    holders = [scenario for scenario in SCENARIOS if scenario.lock_holder is not None]

    assert [scenario.id for scenario in holders] == ["INC-005"]
    holder = holders[0].lock_holder
    assert holder is not None
    assert holder.application_name == "invoice-backfill"
    assert holder.role == "billing_app"
    assert "\n" not in holder.sql
    statements = [part.strip() for part in holder.sql.split(";") if part.strip()]
    assert statements == [
        "BEGIN",
        "SELECT id, status FROM billing.invoices WHERE id = 'inv_kestrel_2002' FOR UPDATE",
    ]
    assert holder.invoice_id == "inv_kestrel_2002"
    for word in ("COMMIT", "ROLLBACK", "UPDATE billing", "INSERT", "DELETE", "DROP", "ALTER"):
        assert word not in holder.sql.upper()


def test_the_watched_request_is_the_first_blocked_payment() -> None:
    scenario = get_scenario("blocked-writes")
    assert scenario.lock_holder is not None
    requests = {item.request_id: item for item in scenario.requests}

    watched = requests[scenario.lock_holder.watched_request]

    assert watched.request_id == scenario.expectation.request_id == scenario.requests[0].request_id
    assert (watched.method, watched.expected_status, watched.expected_code) == (
        "POST",
        503,
        "DATABASE_BUSY",
    )
    assert scenario.lock_holder.invoice_id in watched.path
    assert [item.expected_status for item in scenario.requests] == [503, 200, 503]


def test_the_read_only_cli_never_imports_the_harness() -> None:
    for path in (SRC / "supportops").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            assert not any(name.startswith("supportops_lab") for name in names), path


def test_the_harness_never_names_the_persistent_lab() -> None:
    for path in (SRC / "supportops_lab").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert '"compose.yaml"' not in text, path
        assert "'supportops'" not in text, path
        assert '"supportops"' not in text, path
