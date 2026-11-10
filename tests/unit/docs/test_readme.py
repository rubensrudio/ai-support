"""Final English README (TASK-150, DOD-03, DOD-04, DEMO-04, DEMO-06, STR-06)."""

import re
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[3]
README = ROOT_DIR / "README.md"
REPO_URL = "https://github.com/rubensrudio/ai-support"
SECTIONS = ("Live demo", "Test accounts", "Phases", "Architecture", "Quickstart", "Results")
OPERATOR_MEDIA = {"docs/img/demo.gif"}
KEY_LIKE = re.compile(r"(sk-ant-|sk-proj-|sk-lf-|pk-lf-|ghp_|github_pat_|AKIA)[A-Za-z0-9_-]{4,}")


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.mark.parametrize("title", SECTIONS)
def test_readme_has_the_section(readme: str, title: str) -> None:
    assert re.search(rf"^## {re.escape(title)}$", readme, flags=re.MULTILINE), title


def test_phases_table_links_each_phase_tag_once(readme: str) -> None:
    tags = re.findall(rf"{re.escape(REPO_URL)}/tree/(phase-\d{{2}})", readme)

    assert sorted(set(tags)) == [f"phase-{n:02d}" for n in range(1, 15)]


def test_live_demo_links_the_public_url_and_its_limits(readme: str) -> None:
    section = readme.split("## Live demo", 1)[1].split("\n## ", 1)[0]

    assert "https://atlas-web.onrender.com" in section
    for fact in ("20 messages per hour", "US$ 2", "7 days", "CHAOS_RATE"):
        assert fact in section, fact


def test_live_demo_explains_boot_ingestion_and_the_spend_cap_overshoot(readme: str) -> None:
    section = readme.split("## Live demo", 1)[1].split("\n## ", 1)[0]

    assert "ATLAS_INGEST_ON_BOOT" in section
    assert "OPENAI_API_KEY" in section
    assert "ATLAS_AGENT_MAX_COST_USD" in section
    assert "start of each request" in section


def test_test_accounts_list_both_personas_and_the_local_password(readme: str) -> None:
    section = readme.split("## Test accounts", 1)[1].split("\n## ", 1)[0]

    assert re.search(r"^\|\s*`alex`\s*\|", section, flags=re.MULTILINE)
    assert re.search(r"^\|\s*`bia`\s*\|", section, flags=re.MULTILINE)
    assert "`atlas-demo`" in section
    assert "ATLAS_PERSONA_PASSWORD" in section


def test_results_has_a_ttft_line_for_demo_09(readme: str) -> None:
    lines = [line for line in readme.splitlines() if "TTFT" in line]

    assert lines
    assert any("demo_09" in line or "demos.demo_09_streaming" in line for line in lines)
    assert "docs/results.md" in readme


def test_readme_is_written_in_english(readme: str) -> None:
    langdetect = pytest.importorskip("langdetect")
    langdetect.DetectorFactory.seed = 0
    prose = re.sub(r"```.*?```", " ", readme, flags=re.DOTALL)

    assert langdetect.detect(prose) == "en"


def test_readme_has_no_literal_secret(readme: str) -> None:
    assert not [match.group(0) for match in KEY_LIKE.finditer(readme)]
    assert "X-Operator-Key:" not in readme


def test_referenced_local_images_exist(readme: str) -> None:
    images = re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", readme)
    images += re.findall(r'<img[^>]+src="([^"]+)"', readme)
    local = {path for path in images if not path.startswith(("http://", "https://"))}

    assert "docs/img/architecture.svg" in local
    assert OPERATOR_MEDIA <= local
    for path in local - OPERATOR_MEDIA:
        assert (ROOT_DIR / path).is_file(), path
