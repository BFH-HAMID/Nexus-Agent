"""Static supply-chain checks for the Windows bootstrap installer."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_installer_source_is_pinned_to_a_full_commit() -> None:
    script = (ROOT / "install.ps1").read_text(encoding="utf-8")

    assert re.search(r"^\$NexusCommit = '[0-9a-f]{40}'$", script, re.MULTILINE)
    assert (
        "$PackageSpec = \"git+https://github.com/BFH-HAMID/Nexus-Agent.git@$NexusCommit\""
        " + '#egg=nexus-agent'"
    ) in script
    assert "'--src', $SourceRoot, '--editable', $PackageSpec" in script
    assert "$SourceCommit -ne $NexusCommit" in script


def test_documented_installer_url_is_raw_and_commit_pinned() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    urls = re.findall(r"https://raw\.githubusercontent\.com/[^\s'`)]+", readme)

    assert urls == [
        "https://raw.githubusercontent.com/BFH-HAMID/Nexus-Agent/"
        "4670f4b05dfb767cdcaff0b16ca3f9a2490ed1d2/install.ps1"
    ]
    assert "/main/" not in urls[0]
