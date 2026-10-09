import os
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from supportops.db.runner import CheckResult
from supportops.errors import ConfigError, ExitCode
from supportops.health import HealthReport, Verdict
from supportops.http_checks import HttpResult, create_client
from supportops.investigation import live
from supportops.investigation.engine import investigate
from supportops.investigation.models import Investigation
from supportops.investigation.reporting import (
    ReportLeakError,
    configured_secrets,
    ensure_maskable,
    mask_secrets,
    render_report,
    write_report,
)
from supportops.investigation.text import current_state_notes
from supportops.logs.parser import read_logs
from supportops.settings import Settings
from supportops.targets import BILLING as BILLING_PROFILE
from tests.unit.fake_api import FakeApi
from tests.unit.investigation_support import (
    API_KEY,
    BILLING,
    DB_PASSWORD,
    DB_URL,
    FIXTURES,
    NO_WINDOW,
    NOW,
    FakeChecks,
    check_result,
    invoice_row,
    key_row,
    live_evidence,
)

GOLDEN = FIXTURES / "reports"


def health_report() -> HealthReport:
    liveness = HttpResult(
        method="GET", url="http://127.0.0.1:8001/health", request_id="x", duration_ms=1, status=200
    )
    return HealthReport(
        target="billing",
        api_url="http://127.0.0.1:8001/",
        verdict=Verdict.HEALTHY,
        diagnosis="api_ready",
        summary="ready",
        liveness=liveness,
    )


REGENERATE = os.environ.get("REGENERATE_GOLDEN") == "1"
PARTIAL_INVOICE = "inv_juniper_1005"


def build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    request_id: str,
    checks: dict[str, CheckResult] | None = None,
) -> Investigation:
    shutil.copy(BILLING, tmp_path / "billing-api.jsonl")
    monkeypatch.setattr(live, "run_checks", FakeChecks(checks))
    settings = Settings(db_url=SecretStr(DB_URL))
    with create_client(settings, transport=FakeApi().transport) as client:
        return investigate(
            read_logs(["billing-api.jsonl"]),
            request_id,
            NO_WINDOW,
            settings,
            BILLING_PROFILE,
            client,
            now=lambda: NOW,
        )


def revoked_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Investigation:
    return build(
        monkeypatch,
        tmp_path,
        "demo-401-revoked",
        {"billing.api_key_status": check_result("billing.api_key_status", [key_row()])},
    )


def partial_payment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Investigation:
    record = invoice_row(
        invoice_id=PARTIAL_INVOICE,
        number="INV-1005",
        total_cents=12000,
        line_total_cents=12000,
        succeeded_payments=1,
        failed_payments=0,
    )
    return build(
        monkeypatch,
        tmp_path,
        "demo-500-a",
        {
            "billing.invoice_lookup": check_result("billing.invoice_lookup", [record]),
            "billing.payment_on_unpaid_invoice": check_result(
                "billing.payment_on_unpaid_invoice",
                [{"invoice_id": PARTIAL_INVOICE}, {"invoice_id": "inv_juniper_1006"}],
            ),
        },
    )


@pytest.mark.parametrize(
    ("name", "builder"),
    [("revoked-key", revoked_key), ("partial-payment", partial_payment)],
)
def test_report_matches_the_golden_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, builder: Any
) -> None:
    golden = GOLDEN / f"{name}.md"
    written = write_report(builder(monkeypatch, tmp_path), tmp_path / "out" / f"{name}.md", [])
    text = written.read_bytes().decode("utf-8")
    if REGENERATE:
        golden.write_bytes(text.encode("utf-8"))

    assert text == golden.read_text(encoding="utf-8")


def test_the_draft_banner_and_every_citation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    investigation = partial_payment(monkeypatch, tmp_path)

    text = render_report(investigation)

    assert text.startswith("# Internal investigation draft: request `demo-500-a`\n")
    assert "**Internal investigation draft - not for direct customer distribution.**" in text
    assert "information about other accounts" in text
    assert text.rstrip().endswith("customer-facing decision.*")
    cited = set(re.findall(r"\[(E\d+)\]", text))
    listed = set(re.findall(r"- \*\*(E\d+)\*\*", text))
    assert cited
    assert cited == listed
    assert [item.rule for item in investigation.findings] == [
        "payment_invoice_inconsistent",
        "unhandled_exception",
    ]
    assert "not attributed to this request" in text
    assert "**Lab context:**" in text
    assert "(s)" not in text
    assert "finding(s)" not in text


def test_shared_coverage_gaps_are_explained_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text = render_report(partial_payment(monkeypatch, tmp_path))

    assert text.count("The logs that were read begin 9 min after the start") == 1
    assert text.count("**Coverage:** partial for every signature above") == 1
    assert text.count("**Window:**") == 1


def test_next_steps_are_not_repeated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    text = render_report(partial_payment(monkeypatch, tmp_path))

    section = text.split("## Recommended next steps", 1)[1].split("## Escalation", 1)[0]
    assert section.count("not to retry") == 1
    assert text.count("- **Engineering**, high severity") == 1


def test_another_accounts_details_stay_out_of_customer_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = invoice_row(
        invoice_id="inv_kestrel_2001", number="INV-2001", account_id="acct_kestrel"
    )
    investigation = build(
        monkeypatch,
        tmp_path,
        "demo-404",
        {"billing.invoice_lookup": check_result("billing.invoice_lookup", [record])},
    )

    text = render_report(investigation)

    steps = text.split("## Recommended next steps", 1)[1].split("## Escalation", 1)[0]
    assert investigation.findings[0].rule == "resource_not_found"
    assert "not for direct customer distribution" in text
    assert "acct_kestrel" in text
    assert "acct_kestrel" not in steps
    assert "Internal only: don't tell the customer" in text


@pytest.mark.parametrize(
    ("database", "health", "expected"),
    [
        ("not_needed", False, []),
        ("disabled", False, []),
        ("checked", False, ["Database checks ran at 2026-10-08T12:30:00.000Z."]),
        ("not_needed", True, ["The API health check ran at 2026-10-08T12:30:00.000Z."]),
        (
            "checked",
            True,
            [
                "Database checks ran at 2026-10-08T12:30:00.000Z.",
                "The API health check ran at 2026-10-08T12:30:00.000Z.",
            ],
        ),
    ],
    ids=["offline", "no-db", "database-only", "health-only", "both"],
)
def test_current_state_notes_only_for_checks_that_ran(
    database: str, health: bool, expected: list[str]
) -> None:
    fields: dict[str, Any] = {"database": database, "database_target": "x@db/billing"}
    if health:
        fields["health"] = health_report()

    notes = current_state_notes(live_evidence(**fields))

    assert [note.split(" They ")[0].split(" It ")[0] for note in notes] == expected


def test_an_offline_report_makes_no_current_state_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    investigation = build(monkeypatch, tmp_path, "demo-400")

    text = render_report(investigation)

    assert investigation.live.database == "not_needed"
    assert "- **Database:** not needed for this request." in text
    assert "- **API health:** not checked." in text
    assert "Current state" not in text
    assert "not when the request was made" not in text


def test_an_inconclusive_report(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    investigation = build(monkeypatch, tmp_path, "no-such-request")

    text = render_report(investigation)

    assert investigation.exit_code == ExitCode.PROBLEM
    assert "INCONCLUSIVE" in text
    assert "No rule matched" in text
    assert "No log entries carry this request ID." in text


def test_signature_placeholders_survive_markdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text = render_report(partial_payment(monkeypatch, tmp_path))

    signature = re.search(r"`(unhandled_exception: [^`]*)`", text)
    assert signature is not None
    assert "payment <id> was committed" in signature.group(1)
    outside_code = re.sub(r"`[^`]*`", "", text)
    assert "<id>" not in outside_code.replace("\\<id>", "")


def test_the_leak_guard_refuses_to_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    investigation = revoked_key(monkeypatch, tmp_path).model_copy(
        update={"notes": [f"login failed for {DB_PASSWORD}"]}
    )
    target = tmp_path / "leak.md"

    with pytest.raises(ReportLeakError) as excinfo:
        write_report(investigation, target, configured_secrets(Settings(db_url=SecretStr(DB_URL))))

    assert excinfo.value.exit_code == ExitCode.INCOMPLETE
    assert "the password in SUPPORTOPS_DB_URL" in excinfo.value.message
    assert DB_PASSWORD not in excinfo.value.message
    assert not target.exists()


def test_masking_removes_configured_secrets_everywhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    investigation = revoked_key(monkeypatch, tmp_path).model_copy(
        update={"notes": [f"key {API_KEY} and password {DB_PASSWORD}"]}
    )
    secrets = configured_secrets(Settings(api_key=SecretStr(API_KEY), db_url=SecretStr(DB_URL)))

    masked = mask_secrets(investigation, secrets)

    dumped = masked.model_dump_json()
    assert API_KEY not in dumped
    assert DB_PASSWORD not in dumped
    assert "key bk_juniper01*** and password ***" in masked.notes
    assert masked.findings == investigation.findings


def test_configured_secrets_cover_encoded_passwords() -> None:
    settings = Settings(
        db_url=SecretStr("postgresql://supportops_ro:p%40ss%25word@db:5432/billing")
    )

    assert [value for _, value in configured_secrets(settings)] == ["p%40ss%25word", "p@ss%word"]


SHORT_DB_URL = "postgresql://supportops_ro:x9Q@db:5432/billing"


def test_short_secrets_are_no_longer_ignored() -> None:
    settings = Settings(api_key=SecretStr("k7"), db_url=SecretStr(SHORT_DB_URL))

    assert configured_secrets(settings) == [
        ("SUPPORTOPS_API_KEY", "k7"),
        ("the password in SUPPORTOPS_DB_URL", "x9Q"),
    ]


def test_a_short_secret_in_the_evidence_no_longer_reaches_a_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    investigation = revoked_key(monkeypatch, tmp_path).model_copy(
        update={"notes": ["login failed for x9Q"]}
    )
    secrets = configured_secrets(Settings(db_url=SecretStr(SHORT_DB_URL)))
    target = tmp_path / "short.md"

    with pytest.raises(ConfigError) as excinfo:
        write_report(mask_secrets(investigation, secrets), target, secrets)

    assert excinfo.value.exit_code == ExitCode.USAGE
    assert "too short to redact safely" in excinfo.value.message
    assert "x9Q" not in excinfo.value.message
    assert excinfo.value.hint is not None
    assert "x9Q" not in excinfo.value.hint
    assert not target.exists()


def test_a_short_secret_fails_closed_even_when_it_is_absent() -> None:
    secrets = configured_secrets(Settings(api_key=SecretStr("k7"), db_url=SecretStr(SHORT_DB_URL)))

    with pytest.raises(ConfigError) as excinfo:
        ensure_maskable(secrets)

    assert excinfo.value.message.startswith(
        "A configured secret (SUPPORTOPS_API_KEY and the password in SUPPORTOPS_DB_URL) is too "
        "short to redact safely"
    )
    assert "k7" not in excinfo.value.message
    assert "x9Q" not in excinfo.value.message


def test_secrets_of_four_or_more_characters_pass_the_guard() -> None:
    ensure_maskable(
        configured_secrets(Settings(api_key=SecretStr("k7k7"), db_url=SecretStr(DB_URL)))
    )


def test_short_secrets_are_not_replaced_inside_ordinary_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    note = "HTTP 200 after 12 ms; about 1 request"
    investigation = revoked_key(monkeypatch, tmp_path).model_copy(update={"notes": [note]})
    secrets = [("the password in SUPPORTOPS_DB_URL", "200"), ("SUPPORTOPS_API_KEY", "ab")]

    masked = mask_secrets(investigation, secrets)

    assert masked.notes == [note]
    assert masked == investigation


def test_four_character_secrets_are_still_masked_everywhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    investigation = revoked_key(monkeypatch, tmp_path).model_copy(
        update={"notes": ["password pw12 and token xpw12x"]}
    )
    secrets = [("the password in SUPPORTOPS_DB_URL", "pw12")]

    masked = mask_secrets(investigation, secrets)

    assert masked.notes == ["password *** and token x***x"]
    assert write_report(masked, tmp_path / "ok.md", secrets).exists()
