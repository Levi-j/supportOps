import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from supportops.cli.main import app
from tests.integration.orderflow.support import DEFECTS, OrderflowDatabase, admin_execute

pytestmark = pytest.mark.integration

LOG = str(Path(__file__).resolve().parents[1] / "fixtures" / "logs" / "orderflow-ecs.jsonl")
runner = CliRunner()


def investigate(database: OrderflowDatabase, request_id: str, *args: str) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["investigate", request_id, LOG, *args],
        env={
            "COLUMNS": "200",
            "SUPPORTOPS_TARGET": "orderflow",
            "SUPPORTOPS_DB_URL": database.url(),
        },
    )
    assert database.role_password not in result.output
    return result.exit_code, result.stdout


def findings(stdout: str) -> list[dict[str, Any]]:
    data: dict[str, Any] = json.loads(stdout)
    result: list[dict[str, Any]] = data["findings"]
    return result


def test_an_inconsistent_order_is_confirmed_now_without_claiming_the_cause(
    orderflow_db: OrderflowDatabase,
) -> None:
    admin_execute(orderflow_db, "UPDATE orders SET total_amount = 99.00 WHERE id = 3")

    exit_code, stdout = investigate(orderflow_db, "of-confirm-500", "--json")

    report = json.loads(stdout)
    assert exit_code == 0, stdout
    rules = [item["rule"] for item in report["findings"]]
    assert rules == ["order_inconsistent", "unhandled_exception"]
    order = report["findings"][0]
    assert order["confidence"] == "confirmed"
    assert order["inferences"][0].startswith("Cause, possible association only")
    assert order["escalation"]["team"] == "Engineering / data owner"
    references = {item["reference"] for item in report["evidence"]}
    assert {"orderflow.order_lookup", "orderflow.order_total_mismatch"} <= references
    assert not any(reference.startswith("billing.") for reference in references)
    assert report["live"]["order"]["corroborated_by"] == ["orderflow.order_total_mismatch"]


def test_a_consistent_order_leaves_only_the_logged_exception(
    orderflow_db: OrderflowDatabase,
) -> None:
    exit_code, stdout = investigate(orderflow_db, "of-confirm-500", "--json")

    assert exit_code == 0, stdout
    assert [item["rule"] for item in findings(stdout)] == ["unhandled_exception"]


def test_a_planted_movement_gap_is_found_through_the_order_lookup(
    orderflow_db: OrderflowDatabase,
) -> None:
    admin_execute(
        orderflow_db,
        "DELETE FROM inventory_movements WHERE order_id = 3 AND reason = 'ORDER_PLACED'",
    )

    exit_code, stdout = investigate(orderflow_db, "of-read-200", "--json")

    [order] = [item for item in findings(stdout) if item["rule"] == "order_inconsistent"]
    assert exit_code == 0, stdout
    assert order["confidence"] == "confirmed"
    assert "ORDER_PLACED stock movements don't match the units ordered" in order["summary"]
    assert "only read order 3" in order["inferences"][0]


def test_the_report_draft_names_the_order_and_no_secrets(
    orderflow_db: OrderflowDatabase, tmp_path: Path
) -> None:
    admin_execute(orderflow_db, DEFECTS["orderflow.order_total_mismatch"])
    draft = tmp_path / "of-place.md"

    exit_code, _ = investigate(orderflow_db, "of-place-201", "--report", str(draft))

    text = draft.read_text(encoding="utf-8")
    assert exit_code == 0
    assert "| Order | `1` |" in text
    assert "Order 1 has inconsistent order or stock data (confidence: confirmed)" in text
    assert "Cause not established" in text
    assert orderflow_db.role_password not in text
    assert "billing." not in text


def test_without_the_database_nothing_about_the_order_is_claimed(
    orderflow_db: OrderflowDatabase,
) -> None:
    admin_execute(orderflow_db, DEFECTS["orderflow.order_total_mismatch"])

    exit_code, stdout = investigate(orderflow_db, "of-confirm-500", "--json", "--no-db")

    report = json.loads(stdout)
    assert exit_code == 0
    assert [item["rule"] for item in report["findings"]] == ["unhandled_exception"]
    assert report["live"]["database"] == "disabled"
