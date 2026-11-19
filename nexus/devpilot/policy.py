"""Operator policy for DevPilot (``devpilot.yaml``)."""
from __future__ import annotations

import re
import shlex
from typing import Any

from .. import policyfile

NAME = "devpilot.yaml"
PROTECT = [".github/**", ".gitlab-ci.yml", ".circleci/**", "Jenkinsfile", ".pre-commit-config.yaml", ".husky/**",
           ".gitmodules", ".gitattributes", ".envrc", ".vscode/**", ".devcontainer/**"]
DEFAULTS = {
    "schema": 1,
    "enabled": False,
    "agent": "",
    "allow_cloud": False,
    "max_iterations": 4,
    "max_tokens_per_call": 4000,
    "max_cost_usd": 0.5,
    "timeout_seconds": 1800,
    "checks": {"test": "", "lint": "", "timeout_seconds": 600, "baseline": True, "link": []},
    "context": {"max_files": 12, "max_bytes": 120_000, "listing": 400},
    "patch": {"max_files": 8, "max_file_bytes": 200_000, "allow_new_files": True, "allow_delete": False,
              "allow": [], "protect": list(PROTECT)},
    "apply": {"require_passing_checks": True},
    "keep_runs": 10,
}
_SHELL_META = re.compile(r"[;&|`$><\n\r]")
_RELATIVE = re.compile(r"^(?![/~\\])[^\x00-\x1f\\:]{1,200}$")


class DevPilotPolicyError(policyfile.PolicyError):
    """devpilot.yaml is invalid or DevPilot is OFF."""


def _relative(value: str, where: str) -> None:
    if not _RELATIVE.match(value) or ".." in value.split("/") or value.split("/")[0].lower() == ".git":
        raise DevPilotPolicyError(f"{NAME}: '{where}' entries are repository-relative (no '..', no absolute paths)")


def _validate(cfg: dict[str, Any]) -> None:
    for kind in ("test", "lint"):
        command = cfg["checks"][kind].strip()
        if not command or command.lower() == "none":
            continue
        if len(command) > 300 or _SHELL_META.search(command):
            raise DevPilotPolicyError(
                f"{NAME}: 'checks.{kind}' is one plain command (no pipes, ';', '&&' or redirects) - put pipelines in a script")
        try:
            parts = shlex.split(command)
        except ValueError:
            parts = []
        if not parts:
            raise DevPilotPolicyError(f"{NAME}: 'checks.{kind}' has unbalanced quotes")
    for where in ("checks.link", "patch.allow", "patch.protect"):
        for entry in policyfile.get(cfg, where):
            _relative(entry, where)


POLICY = policyfile.Policy(
    NAME, DEFAULTS, label="DevPilot", error=DevPilotPolicyError, validate=_validate,
    ranges={"max_iterations": (1, 10), "max_tokens_per_call": (500, 16_000), "max_cost_usd": (0.0, 50.0),
            "timeout_seconds": (30, 14_400), "checks.timeout_seconds": (5, 7200), "context.max_files": (1, 40),
            "context.max_bytes": (2000, 600_000), "context.listing": (0, 2000), "patch.max_files": (1, 40),
            "patch.max_file_bytes": (1000, 500_000), "keep_runs": (1, 100)},
    list_limits={"checks.link": 8, "patch.allow": 32, "patch.protect": 32},
)
load = POLICY.load
require = POLICY.require
enabled = POLICY.enabled
digest = policyfile.digest
