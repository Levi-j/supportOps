import re
from collections import Counter
from pathlib import Path

import pytest

from supportops.settings import load_settings
from supportops.targets import get_target

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
EXTERNAL = ("http://", "https://", "mailto:")


def prose_lines(path: Path) -> list[str]:
    lines, fenced = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(line)
    return lines


def anchors(path: Path) -> set[str]:
    seen: Counter[str] = Counter()
    result = set()
    for line in prose_lines(path):
        match = HEADING.match(line)
        if match is None:
            continue
        text = re.sub(r"[^\w\- ]", "", match.group(2).replace("`", "").lower())
        slug = text.replace(" ", "-")
        result.add(slug if not seen[slug] else f"{slug}-{seen[slug]}")
        seen[slug] += 1
    return result


def links(path: Path) -> list[str]:
    return [target for line in prose_lines(path) for target in LINK.findall(line)]


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: str(path.relative_to(ROOT)))
def test_relative_links_and_anchors_resolve(document: Path) -> None:
    for target in links(document):
        if target.startswith(EXTERNAL):
            continue
        location, _, anchor = target.partition("#")
        resolved = (document.parent / location).resolve() if location else document
        assert resolved.exists(), f"{document.name}: missing target {target}"
        if anchor and resolved.suffix == ".md":
            assert anchor in anchors(resolved), f"{document.name}: missing anchor {target}"


def test_the_anchor_rules_match_github() -> None:
    sample = ROOT / "docs" / "incidents" / "README.md"

    assert "incident-index" in anchors(sample)
    assert "resetting-the-lab" in anchors(ROOT / "README.md")
    assert "sample-investigation-a-payment-blocked-by-a-database-transaction" in anchors(
        ROOT / "README.md"
    )


def test_public_documents_do_not_mention_internal_workflow() -> None:
    for document in [*DOCUMENTS, ROOT / "orderflow.env.example", ROOT / ".env.example"]:
        text = document.read_text(encoding="utf-8")
        assert "CLAUDE" not in text, document
        assert not re.search(r"\bM(?:[1-9]|10)\b", text), document


def test_the_orderflow_example_contains_no_credentials() -> None:
    text = (ROOT / "orderflow.env.example").read_text(encoding="utf-8")
    settings = [line for line in text.splitlines() if line and not line.startswith("#")]

    assert settings == [
        "SUPPORTOPS_TARGET=orderflow",
        "SUPPORTOPS_API_URL=http://127.0.0.1:8080",
        "SUPPORTOPS_LOG_SOURCE=docker:orderflow-backend-app-1",
    ]
    assert "eyJ" not in text
    assert "<password>" in text


def test_the_orderflow_example_selects_the_read_only_target() -> None:
    settings = load_settings(ROOT / "orderflow.env.example").settings

    assert settings.target == "orderflow"
    assert str(settings.api_url) == "http://127.0.0.1:8080/"
    assert (settings.api_key, settings.db_url) == (None, None)
    assert get_target(settings.target).lab_writes_allowed is False
