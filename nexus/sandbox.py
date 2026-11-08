"""Docker sandbox policy parsing, hardened argv assembly, and readiness checks."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

IMAGE_DIGEST_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,150}@sha256:[a-f0-9]{64}$"
)
MEMORY_RE = re.compile(r"^[1-9][0-9]{0,5}[kmgKMG]?$")

SANDBOX_DEFAULTS: dict[str, Any] = {
    "network": "none",
    "read_only": True,
    "memory": "512m",
    "cpus": 1,
    "pids_limit": 128,
    "timeout_seconds": 60,
    "workspace_only": True,
}


def parse_sandbox_policy(value: Any) -> dict[str, Any]:
    """Validate optional ``sandbox:`` policy block from ``permissions.yaml``."""
    if value is None:
        return dict(SANDBOX_DEFAULTS)
    if not isinstance(value, dict):
        raise ValueError("Invalid sandbox policy: expected a mapping")
    unknown = set(value) - set(SANDBOX_DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown sandbox policy field(s): {', '.join(sorted(unknown))}")
    cfg = {**SANDBOX_DEFAULTS, **value}
    if not isinstance(cfg["network"], str) or cfg["network"] != "none":
        raise ValueError("Sandbox requires network: none")
    for flag in ("read_only", "workspace_only"):
        if type(cfg[flag]) is not bool or cfg[flag] is not True:
            raise ValueError(f"Sandbox requires {flag}: true")
    if not isinstance(cfg["memory"], str) or not MEMORY_RE.fullmatch(cfg["memory"]):
        raise ValueError("Invalid sandbox memory limit (example: 512m)")
    cpus = cfg["cpus"]
    if type(cpus) not in (int, float) or isinstance(cpus, bool) or not 0 < float(cpus) <= 16:
        raise ValueError("Sandbox cpus must be a finite number between 0 and 16")
    pids = cfg["pids_limit"]
    if type(pids) is not int or not 1 <= pids <= 4096:
        raise ValueError("Sandbox pids_limit must be an integer between 1 and 4096")
    timeout = cfg["timeout_seconds"]
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise ValueError("Sandbox timeout_seconds must be an integer between 1 and 600")
    return cfg


def docker_host() -> str:
    return "npipe:////./pipe/docker_engine" if os.name == "nt" else "unix:///var/run/docker.sock"


def docker_run_argv(
    image: str,
    source_dir: Path | str,
    ident: str,
    command: str,
    policy: dict[str, Any] | None = None,
    *,
    docker_bin: str = "docker",
    host_arg: str | None = None,
) -> list[str]:
    """Construct the hardened container invocation argv used by ``computer.sandbox``."""
    cfg = parse_sandbox_policy(policy)
    host = host_arg or docker_host()
    argv = [
        docker_bin,
        "--host",
        host,
        "run",
        "--pull=never",
        "--rm",
        "--name",
        ident,
        f"--network={cfg['network']}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
    ]
    if cfg["read_only"]:
        argv.append("--read-only")
    argv.extend([
        f"--pids-limit={cfg['pids_limit']}",
        f"--memory={cfg['memory']}",
        f"--cpus={cfg['cpus']}",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
        "--user",
        "65534:65534",
        "--mount",
        f"type=bind,source={source_dir},target=/workspace,readonly",
        "--workdir=/workspace",
        "--entrypoint=/bin/sh",
        image,
        "-lc",
        command,
    ])
    return argv


def _verify_argv_invariants(argv: list[str], policy: dict[str, Any]) -> dict[str, tuple[bool, str]]:
    """Inspect generated Docker argv for network/readonly/limits/mount isolation."""
    network_ok = "--network=none" in argv and policy.get("network") == "none"
    readonly_ok = "--read-only" in argv and policy.get("read_only") is True
    mem_ok = any(a.startswith("--memory=") and a.split("=", 1)[1] for a in argv)
    cpu_ok = any(a.startswith("--cpus=") and a.split("=", 1)[1] for a in argv)
    pids_ok = any(a.startswith("--pids-limit=") and a.split("=", 1)[1] for a in argv)
    timeout_val = policy.get("timeout_seconds")
    timeout_ok = type(timeout_val) is int and 1 <= timeout_val <= 600
    limits_ok = mem_ok and cpu_ok and pids_ok and timeout_ok

    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount" and i + 1 < len(argv)]
    bad_flags = [a for a in argv if a in {"-v", "--volume"} or a.startswith("--volume=")]
    mount_ok = (
        policy.get("workspace_only") is True
        and not bad_flags
        and len(mounts) == 1
        and "target=/workspace" in mounts[0]
        and mounts[0].endswith(",readonly")
        and "docker.sock" not in " ".join(mounts)
    )
    return {
        "network_off": (network_ok, "--network=none enforced" if network_ok else "network is not disabled"),
        "root_readonly": (readonly_ok, "--read-only rootfs enforced" if readonly_ok else "root filesystem is not read-only"),
        "resource_limits": (
            limits_ok,
            f"memory={policy.get('memory')}, cpus={policy.get('cpus')}, pids={policy.get('pids_limit')}, timeout={timeout_val}s"
            if limits_ok
            else "missing or invalid memory/CPU/PID/time limits",
        ),
        "workspace_only_mount": (
            mount_ok,
            "single read-only staging copy mounted at /workspace"
            if mount_ok
            else "unexpected extra or writable mounts",
        ),
    }


def _read_raw_policy(workspace: Path) -> tuple[dict[str, Any], str]:
    import yaml

    path = workspace / "permissions.yaml"
    if not path.exists():
        return {}, ""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64_000:
        return {}, "permissions.yaml must be a bounded regular file"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return {}, f"Malformed permissions.yaml: {exc}"
    return (data if isinstance(data, dict) else {}), ("" if isinstance(data, dict) else "permissions.yaml must be a mapping")


def check_sandbox(workspace: Path | str) -> dict[str, Any]:
    """Verify Docker availability, digest-pinned image, and sandbox isolation invariants."""
    from .computer import environment
    from .permissions import Permissions

    root = Path(workspace).resolve()
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, hint: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "hint": hint})

    # 1. Policy inspection (try Permissions.load, fall back to raw YAML for clear diagnostics).
    policy_error = ""
    raw_data, raw_err = _read_raw_policy(root)
    try:
        loaded = Permissions(root).load()
        computer_cfg = loaded["computer"]
        sandbox_cfg = loaded.get("sandbox") or dict(SANDBOX_DEFAULTS)
    except ValueError as exc:
        policy_error = str(exc)
        computer_cfg = raw_data.get("computer") if isinstance(raw_data.get("computer"), dict) else {}
        try:
            sandbox_cfg = parse_sandbox_policy(raw_data.get("sandbox"))
        except ValueError:
            sandbox_cfg = dict(SANDBOX_DEFAULTS)

    shell_backend = str(computer_cfg.get("shell_backend", "host"))
    image = str(computer_cfg.get("image", "") or "")

    # 2. Docker CLI & daemon availability.
    docker_bin = shutil.which("docker")
    docker_ok = False
    docker_detail = "docker binary not found in PATH"
    if docker_bin:
        try:
            proc = subprocess.run(
                [docker_bin, "--host", docker_host(), "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=10,
                env=environment(),
            )
            if proc.returncode == 0:
                docker_ok = True
                version = (proc.stdout or "").strip() or "running"
                docker_detail = f"{docker_bin} (daemon {version})"
            else:
                stderr = (proc.stderr or "").strip().splitlines()
                docker_detail = f"Docker daemon unreachable ({stderr[-1] if stderr else f'exit {proc.returncode}'})"
        except (OSError, subprocess.TimeoutExpired) as exc:
            docker_detail = f"Docker check failed: {exc}"
    add(
        "docker_available",
        docker_ok,
        docker_detail,
        "Install Docker and ensure the daemon is running" if not docker_ok else "",
    )

    # 3. shell_backend: docker
    backend_ok = shell_backend == "docker" and not raw_err
    add(
        "shell_backend",
        backend_ok,
        f"computer.shell_backend = {shell_backend!r}" + (f" ({raw_err})" if raw_err else ""),
        "Set `computer: { shell_backend: docker, image: <repo>@sha256:<64-hex> }` in permissions.yaml"
        if not backend_ok
        else "",
    )

    # 4. Digest-pinned image
    pinned_ok = bool(IMAGE_DIGEST_RE.fullmatch(image))
    add(
        "image_pinned",
        pinned_ok,
        image if pinned_ok else (f"unpinned or empty image: {image!r}" if image else "no image configured"),
        "Pull an image and copy its RepoDigest, e.g. `docker pull python:3.12-slim && "
        "docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim`"
        if not pinned_ok
        else "",
    )

    # 5. Image installed locally (--pull=never invariant)
    installed_ok = False
    installed_detail = "skipped (requires Docker daemon and pinned image)"
    if docker_ok and pinned_ok and docker_bin:
        try:
            insp = subprocess.run(
                [docker_bin, "--host", docker_host(), "image", "inspect", "--format", "{{.Id}}", image],
                capture_output=True,
                text=True,
                timeout=10,
                env=environment(),
            )
            if insp.returncode == 0:
                installed_ok = True
                installed_detail = f"present ({(insp.stdout or '').strip()[:24]})"
            else:
                installed_detail = f"image not installed locally: {image}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            installed_detail = f"image inspect failed: {exc}"
    add(
        "image_installed",
        installed_ok,
        installed_detail,
        f"Pull the pinned image first: `docker pull {image}`" if (pinned_ok and not installed_ok) else "",
    )

    # 6. Sandbox container flags (network off, read-only root, limits, workspace-only mount)
    preview_image = image or "example@sha256:" + "0" * 64
    argv = docker_run_argv(
        preview_image,
        root / ".sandbox-staging",
        "nexus-check-preview",
        "true",
        sandbox_cfg,
        docker_bin=docker_bin or "docker",
    )
    invariants = _verify_argv_invariants(argv, sandbox_cfg)
    raw_sandbox = raw_data.get("sandbox")
    raw_sandbox_err = ""
    if raw_sandbox is not None:
        try:
            parse_sandbox_policy(raw_sandbox)
        except ValueError as exc:
            raw_sandbox_err = str(exc)
    if policy_error and not raw_sandbox_err and "sandbox" in policy_error.lower():
        raw_sandbox_err = policy_error

    for key in ("network_off", "root_readonly", "resource_limits", "workspace_only_mount"):
        ok, detail = invariants[key]
        if raw_sandbox_err:
            ok = False
            detail = f"{raw_sandbox_err}"
        add(key, ok, detail, "Keep default hardened sandbox settings in permissions.yaml" if not ok else "")

    return {
        "ok": all(c["ok"] for c in checks) and not policy_error,
        "workspace": str(root),
        "shell_backend": shell_backend,
        "image": image,
        "policy": sandbox_cfg,
        "policy_error": policy_error,
        "checks": checks,
        "argv_preview": json.loads(json.dumps(argv)),
    }
