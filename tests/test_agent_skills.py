"""Validate portable Agent Skills and client-specific adapters."""

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SKILL_NAMES = ("openocto-jump-hosts", "openocto-tmux")


def _read_skill(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    assert len(parts) == 3 and not parts[0].strip(), f"Invalid frontmatter: {path}"

    metadata = {}
    for line in parts[1].strip().splitlines():
        key, separator, value = line.partition(":")
        assert separator, f"Invalid frontmatter line in {path}: {line}"
        metadata[key.strip()] = value.strip()
    return metadata, parts[2]


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_canonical_agent_skill(skill_name):
    path = ROOT / ".agents" / "skills" / skill_name / "SKILL.md"
    metadata, body = _read_skill(path)

    assert metadata["name"] == skill_name
    assert len(metadata["name"]) <= 64
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", metadata["name"])
    assert 1 <= len(metadata["description"]) <= 1024
    assert 1 <= len(metadata["compatibility"]) <= 500
    assert len(path.read_text(encoding="utf-8").splitlines()) <= 500
    assert "remote_run" in body
    assert "octo run" in body


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_cursor_adapter_points_to_canonical_skill(skill_name):
    adapter = ROOT / ".cursor" / "skills" / skill_name / "SKILL.md"
    metadata, body = _read_skill(adapter)
    canonical = ROOT / ".agents" / "skills" / skill_name / "SKILL.md"
    reference = f"../../../.agents/skills/{skill_name}/SKILL.md"

    assert metadata["name"] == skill_name
    assert reference in body
    assert (adapter.parent / reference).resolve() == canonical.resolve()
    assert canonical.is_file()
