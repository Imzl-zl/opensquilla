"""Contract checks for the local research coordinator and its companions."""

from __future__ import annotations

from pathlib import Path

from opensquilla.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[2]
BUNDLED = ROOT / "src" / "opensquilla" / "skills" / "bundled"
SOURCE = ROOT / "scripts" / "knowledge_research" / "skill" / "SKILL.md"

COMPANIONS = {
    "knowledge-research-finance": ("actual or estimate", "condition", "metric"),
    "knowledge-research-pdf-tables": ("mcp_getFileDetails", "mcp_getTable", "footnotes"),
    "knowledge-research-report": ("mcp_researchFinalize", "provenance.json", 'bundle="none"'),
}


def test_coordinator_points_to_all_companion_skills() -> None:
    coordinator = (BUNDLED / "knowledge-local-research" / "SKILL.md").read_text(encoding="utf-8")
    for name in COMPANIONS:
        assert f"knowledge-research-{name.removeprefix('knowledge-research-')}" in coordinator
    assert 'skill_view(name="knowledge-research-finance")' in coordinator
    assert 'skill_view(name="knowledge-research-pdf-tables")' in coordinator
    assert 'skill_view(name="knowledge-research-report")' in coordinator


def test_companions_are_loadable_and_keep_their_hard_rules() -> None:
    loader = SkillLoader(bundled_dir=BUNDLED)
    for name, required_phrases in COMPANIONS.items():
        spec = loader.get_by_name(name)
        assert spec is not None
        assert all(phrase in spec.content for phrase in required_phrases)


def test_deployed_sidecar_source_matches_bundled_coordinator() -> None:
    bundled = (BUNDLED / "knowledge-local-research" / "SKILL.md").read_text(encoding="utf-8")
    assert SOURCE.read_text(encoding="utf-8") == bundled
