"""Which check commands DevPilot runs, the scrubbed environment they get, and a bounded runner.

The commands come from ``devpilot.yaml`` or from fixed project markers - never
from a model. They are started without a shell.

``{python}`` as a word of a command stands for the project's Python interpreter,
used through a per-run environment (:func:`overlay`) that imports the checkout
instead of the working tree.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

_SECRET_ENV = re.compile(r"(?i)(?:TOKEN|API_?KEY|PRIVATE_KEY|SECRET|PASSWORD|PASSWD|AUTHORIZATION|CREDENTIALS?|_PIN|_KEY)S?$")
_DROP_ENV = {"SSH_AUTH_SOCK", "SSH_ASKPASS", "GPG_AGENT_INFO", "NETRC"}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
HEAD_BYTES, TAIL_BYTES = 2000, 10_000
PYTHON = "{python}"
_OVERLAY = """\
# Written by Nexus DevPilot for one run; deleted with it.
import os
import site
import sys

ROOTS = %(roots)r
SITES = %(sites)r
WORKSPACE, CHECKOUT = %(workspace)r, %(checkout)r
DONE = False


def install():
    global DONE
    if DONE:  # site.py reads the .pth files of a virtualenv twice
        return
    DONE = True
    head = list(sys.path)
    for folder in SITES:  # the project's installed packages, including what its .pth files add
        site.addsitedir(folder)
    kept = {os.path.realpath(folder) for folder in SITES}
    path = []
    for entry in head + ROOTS + sys.path[len(head):]:
        full = os.path.realpath(entry)
        if full not in kept and (full == WORKSPACE or full.startswith(WORKSPACE + os.sep)):
            twin = CHECKOUT + full[len(WORKSPACE):]  # an editable install points into the working tree
            entry = twin if os.path.isdir(twin) else entry
        if entry not in path:
            path.append(entry)
    sys.path[:] = path
"""


def project_python(workspace: Path) -> str:
    """The project's own virtualenv interpreter when it has one, else the interpreter running Nexus."""
    for folder in (".venv", "venv"):
        for parts in (("bin", "python"), ("Scripts", "python.exe")):
            candidate = workspace / folder / parts[0] / parts[1]
            if candidate.is_file():
                return str(candidate)
    return sys.executable


def _text(path: Path, limit: int = 400_000) -> str:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _markers(tree: Path, workspace: Path) -> dict[str, tuple[list[str], str]]:
    def has(name: str) -> bool:
        return (tree / name).is_file()

    found: dict[str, tuple[list[str], str]] = {}
    python_tests = any((tree / folder).is_dir() and any((tree / folder).glob("test_*.py")) for folder in ("tests", "test"))
    if python_tests or any(has(name) for name in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini")):
        found["test"] = ([PYTHON, "-m", "pytest", "-q"], "detected: Python project")
        if has("ruff.toml") or has(".ruff.toml") or "[tool.ruff" in _text(tree / "pyproject.toml"):
            # a linter reads files and imports nothing, so it needs no per-run environment
            found["lint"] = ([project_python(workspace), "-m", "ruff", "check", "."], "detected: ruff configuration")
    elif has("package.json"):
        try:
            scripts = json.loads(_text(tree / "package.json") or "{}").get("scripts") or {}
        except (ValueError, AttributeError):
            scripts = {}
        scripts = scripts if isinstance(scripts, dict) else {}
        manager = "pnpm" if has("pnpm-lock.yaml") else "yarn" if has("yarn.lock") else "npm"
        binary = shutil.which(manager) or manager
        if isinstance(scripts.get("test"), str) and "no test specified" not in scripts["test"]:
            found["test"] = ([binary, "test"], "detected: package.json scripts.test")
        if isinstance(scripts.get("lint"), str):
            found["lint"] = ([binary, "run", "lint"], "detected: package.json scripts.lint")
    elif has("Cargo.toml"):
        found["test"] = (["cargo", "test"], "detected: Cargo.toml")
    elif has("go.mod"):
        found["test"] = (["go", "test", "./..."], "detected: go.mod")
        found["lint"] = (["go", "vet", "./..."], "detected: go.mod")
    elif re.search(r"(?m)^test\s*:", _text(tree / "Makefile")):
        found["test"] = (["make", "test"], "detected: Makefile test target")
    return found


def detect(tree: Path, cfg: dict[str, Any], workspace: Path) -> dict[str, dict[str, Any]]:
    """``{"lint": {...}, "test": {...}}`` with ``argv`` (empty = not run), ``display`` and ``source``.

    ``argv`` keeps the word ``{python}``; ``display`` names the interpreter it stands for.
    """
    found = _markers(tree, workspace)
    python = project_python(workspace)
    out: dict[str, dict[str, Any]] = {}
    for kind in ("lint", "test"):
        configured = cfg["checks"][kind].strip()
        if configured.lower() == "none":
            out[kind] = {"argv": [], "display": "", "source": "off in devpilot.yaml"}
            continue
        if configured:
            argv, source = shlex.split(configured, posix=os.name != "nt"), "devpilot.yaml"
        else:
            argv, source = found.get(kind, ([], "nothing detected"))
        out[kind] = {"argv": list(argv), "source": source,
                     "display": " ".join(shlex.quote(python if part == PYTHON else part) for part in argv)}
    return out


def uses_python(commands: dict[str, dict[str, Any]]) -> bool:
    return any(PYTHON in command["argv"] for command in commands.values())


def overlay(workspace: Path, tree: Path, folder: Path) -> str:
    """Create the per-run Python environment in ``folder`` and return its interpreter.

    It is a virtualenv with no packages of its own. A start-up file adds the
    site-packages of the project's interpreter and puts the checkout in front of
    them, so installed dependencies are found while the project itself is
    imported from the checkout - also when the project is installed in editable
    mode from the working tree, and also in ``python -I`` subprocesses, which
    ignore ``PYTHONPATH``. Raises ``OSError`` or ``ValueError`` when it cannot be built.
    """
    python = project_python(workspace)

    def ask(interpreter: str, code: str) -> str:
        done = subprocess.run([interpreter, "-I", "-c", code], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, env=environment(tree))
        if done.returncode:
            raise ValueError((done.stderr.strip().splitlines() or ["the interpreter failed"])[-1][:200])
        return done.stdout.strip()

    try:
        sites = json.loads(ask(python, "import json, site; print(json.dumps(site.getsitepackages()))"))
        made = subprocess.run([python, "-I", "-m", "venv", "--without-pip", str(folder)], capture_output=True, text=True,
                              timeout=120, stdin=subprocess.DEVNULL, env=environment(tree))
        if made.returncode:
            raise ValueError((made.stderr.strip().splitlines() or ["python -m venv failed"])[-1][:200])
        interpreter = next((str(folder / a / b) for a, b in (("bin", "python"), ("Scripts", "python.exe"))
                            if (folder / a / b).exists()), "")
        if not interpreter or not isinstance(sites, list) or not all(isinstance(item, str) for item in sites):
            raise ValueError("unexpected virtualenv layout")
        packages = Path(ask(interpreter, "import sysconfig; print(sysconfig.get_path('purelib'))"))
        roots = [str(tree)] + ([str(tree / "src")] if (tree / "src").is_dir() else [])
        # a name of its own: the project's interpreter may itself be such an environment (DevPilot testing DevPilot)
        module = "_devpilot_checkout_" + hashlib.sha256(str(folder).encode()).hexdigest()[:12]
        (packages / f"{module}.py").write_text(_OVERLAY % {
            "roots": roots, "sites": sites, "workspace": os.path.realpath(workspace),
            "checkout": os.path.realpath(tree)}, encoding="utf-8")
        (packages / f"{module}.pth").write_text(f"import {module}; {module}.install()\n", encoding="utf-8")
    except subprocess.SubprocessError as exc:
        raise ValueError(type(exc).__name__) from None
    return interpreter


def environment(tree: Path) -> dict[str, str]:
    """The caller's environment without credentials, with the checkout first on ``PYTHONPATH``.

    Putting the checkout first makes Python import the patched copy even when
    the project is installed in editable mode from the real working tree.
    """
    env = {key: value for key, value in os.environ.items()
           if not _SECRET_ENV.search(key) and key not in _DROP_ENV and not key.startswith("GIT_")}
    roots = [str(tree)] + ([str(tree / "src")] if (tree / "src").is_dir() else [])
    env["PYTHONPATH"] = os.pathsep.join(roots + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1", GIT_TERMINAL_PROMPT="0", PAGER="cat")
    return env


def run_check(command: dict[str, Any], tree: Path, *, timeout: float, workspace: Path | None = None,
              cancel: threading.Event | None = None, python: str = "") -> dict[str, Any]:
    """Run one check in the checkout. Keeps the first and the last part of its output.

    ``status`` is ``passed`` (exit 0), ``failed``, ``timeout``, ``cancelled`` or
    ``error`` (the command could not be started). ``python`` replaces the word
    ``{python}``; without it the project's own interpreter is used.
    """
    from ..computer_control import EmergencyStop
    from ..live_process import terminate_group
    from ..redaction import mask, secrets

    result: dict[str, Any] = {"command": command["display"], "status": "error", "exit_code": None, "seconds": 0.0,
                              "output": ""}
    emergency = EmergencyStop()
    if emergency.stopped() or (cancel is not None and cancel.is_set()):
        return {**result, "status": "cancelled", "output": "cancelled before start"}
    started = time.monotonic()
    try:
        python = python or project_python(workspace or tree)
        process = subprocess.Popen([python if part == PYTHON else part for part in command["argv"]],
                                   cwd=str(tree), env=environment(tree), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=os.name == "posix")
    except OSError as exc:
        return {**result, "output": f"could not start '{command['display'].split(' ')[0]}': {exc.strerror or exc}"}
    box = {"head": bytearray(), "tail": bytearray(), "total": 0}

    def pump() -> None:
        try:
            while True:
                data = process.stdout.read1(65_536)
                if not data:
                    return
                box["total"] += len(data)
                room = HEAD_BYTES - len(box["head"])
                if room > 0:
                    box["head"] += data[:room]
                    data = data[room:]
                box["tail"] += data
                if len(box["tail"]) > 4 * TAIL_BYTES:
                    del box["tail"][:-TAIL_BYTES]
        except (OSError, ValueError):
            return

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    status = ""
    try:
        while process.poll() is None:
            if emergency.stopped() or (cancel is not None and cancel.is_set()):
                status = "cancelled"
                break
            if time.monotonic() - started >= timeout:
                status = "timeout"
                break
            time.sleep(0.05)
    finally:
        terminate_group(process)  # also reaps children that outlive the command
        reader.join(timeout=3)
        try:
            process.stdout.close()
        except OSError:
            pass
    tail = bytes(box["tail"][-TAIL_BYTES:])
    omitted = box["total"] - len(box["head"]) - len(tail)
    text = bytes(box["head"]).decode("utf-8", "replace")
    if omitted > 0:
        text += f"\n[... {omitted} bytes omitted ...]\n"
    text = _ANSI.sub("", text + tail.decode("utf-8", "replace"))
    result.update(seconds=round(time.monotonic() - started, 2), output=mask(text, secrets(workspace)).strip())
    if status:
        note = f"timed out after {int(timeout)}s" if status == "timeout" else "cancelled"
        return {**result, "status": status, "output": (result["output"] + f"\n[{note}]").strip()}
    return {**result, "status": "passed" if process.returncode == 0 else "failed", "exit_code": process.returncode}
