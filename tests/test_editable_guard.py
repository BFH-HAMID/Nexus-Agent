"""Verify the pytest editable-install guard catches non-editable environments."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests import conftest as test_conftest


def test_editable_install_guard_accepts_current_checkout():
    test_conftest._check_editable_install(test_conftest.REPO_ROOT)


def test_editable_install_guard_rejects_missing_and_non_editable(monkeypatch, tmp_path: Path):
    def missing(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["python", "-I"],
            returncode=1,
            stdout="",
            stderr="ModuleNotFoundError: No module named 'nexus'\n",
        )

    monkeypatch.setattr(test_conftest.subprocess, "run", missing)
    with pytest.raises(pytest.UsageError, match='pip install -e "\\.\\[dev\\]"'):
        test_conftest._check_editable_install(tmp_path)

    site_pkg = tmp_path / "site-packages" / "nexus" / "__init__.py"

    def non_editable(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["python", "-I"],
            returncode=0,
            stdout=f"{site_pkg}\n",
            stderr="",
        )

    monkeypatch.setattr(test_conftest.subprocess, "run", non_editable)
    with pytest.raises(pytest.UsageError, match="editable mode") as excinfo:
        test_conftest._check_editable_install(tmp_path)
    assert str(site_pkg) in str(excinfo.value)
