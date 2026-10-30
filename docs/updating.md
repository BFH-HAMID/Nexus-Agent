# Updating Nexus from GitHub

Once this updater version is installed, run in your existing Nexus terminal/environment:

```powershell
nexus update
```

This is explicit consent to download and **execute trusted upstream code/build hooks and dependency installers**. The source is fixed to `BFH-HAMID/Nexus-Agent`, branch `main`; no arbitrary URL, branch, token or shell command is accepted. The updater does not start itself or run on a schedule.

## Git checkout installations

The installed source checkout—not the current working project—must be on `main`, have the official origin, and have no tracked changes or untracked files. The updater fetches the official HTTPS source, requires the current commit to be an ancestor of fetched main, then fast-forwards. Detached, foreign, feature, dirty, locally-ahead and diverged checkouts refuse rather than switching branches, stashing, resetting, cleaning or merging conflicts. Ignored files are protected with `--no-overwrite-ignore`.

It installs the checkout editable using **the same `sys.executable` Python environment** and pip, installing required base dependencies. Neither a bare system `pip` nor administrator elevation is used. If you keep personal configuration edits or an untracked `app/` in the source checkout, it will intentionally refuse; back up/commit your files yourself, or use a separate clean installation. It never deletes them for you.

## Package/pip installations

No Git checkout or Git executable is required. The updater resolves the official main SHA through the public GitHub API and installs the pinned archive with pip. Force reinstall ensures code changes are applied even if the package version has not changed. Base dependencies may be upgraded/reinstalled; this is best used in a dedicated virtual environment or pipx environment, not a shared/system interpreter. Externally managed environments may refuse; there is no `--break-system-packages` override.

Optional extras cannot be reliably reconstructed from pip metadata. Existing unrelated packages are not intentionally removed, but to enforce current optional requirements select them explicitly each time:

```powershell
nexus update --extras hud,mcp,context
```

Other supported groups: `computer`, `orchestrator`, `secure-state`, `anthropic`, `dev`. Base-only `nexus update` does not promise to upgrade every optional integration.

## Running processes and Windows

Stop GUI/CLI workers, Telegram polling and OS startup services **before updating**; restart after success. Local standard `nexus`/`nexus.exe` and `python -m nexus...` processes are checked best-effort. Hidden/inaccessible/custom launchers, other machines/users and workers starting concurrently cannot be reliably detected; this is not a universal service supervisor or automatic stop/restart.

On Windows the command schedules a copied Python worker in `~/.config/nexus/update-jobs/`. The worker waits for the CLI parent to exit, avoiding replacing a running `nexus.exe` launcher. **Scheduled is not completed**: the worker prints its actual outcome and stores `result.json` (`ok` plus step messages) in the printed job directory. A nonzero worker result is failure; the scheduling command's exit code is not update acceptance. If using `/update` in the REPL, exit the REPL when instructed; otherwise the worker times out without updating. Job receipts contain local paths/status, not command output or credentials; you may delete old job directories after reviewing results. Windows behaviour is structurally tested, not physically validated in this Linux sandbox.

After installation, `pip check` and an isolated fresh-process CLI import check run. This proves only dependency consistency/basic import, not live credentials, provider health or GUI acceptance. Restart the old agent processes; loaded code does not hot-reload.

## Check and recovery

```powershell
nexus update --check
```

This queries source but does not fetch/merge/install. For Git it compares HEAD to upstream; for package installs it reports the available SHA without claiming equivalence to the installed revision. It still creates/acquires a local updater lock.

Maintenance-only options (Git checkout): `--no-pull` reinstalls the current clean official-origin main checkout without fetching or verifying that its commit exists upstream; `--no-install` updates code only and explicitly skips dependencies/health checks. These are not a full update.

A per-OS-user lock prevents simultaneous updater workers, independent of `NEXUS_HOME`. API keys, `.env`, native vault, memory/history and workspace state are not migration/cleanup targets. Personal changes to tracked source config block updates instead of being overwritten. Clean tracked defaults can change with upstream; keep personalised workspace configuration outside the clean installation checkout.

Failures are **not transactional**: a Git fast-forward or partial pip/dependency changes may have happened already. The receipt reports completed stages and incomplete state; no automatic rollback or destructive recovery is attempted. Keep Nexus stopped, resolve the problem, and retry. Raw subprocess/network output is withheld to avoid leaking private URLs/environment data; use your normal trusted Git/pip diagnostics locally if needed.

An old Nexus installation must receive this updater once using its existing install method (for example update a backed-up clean clone and run `python -m pip install -e .`). It cannot learn a new command implementation without first installing the commit containing it.

## Implementation validation

Full Python regression: **1,367 passed, 1 optional browser test skipped** (118.97s; one upstream deprecation warning). Updater/CLI-focused tests: **36 passed**. Tests script fetch/pip/process observations, and also use real temporary local Git repositories to verify fast-forward and refusal to overwrite an ignored `.env`. They never install an update into the working checkout. Clean base-only wheel checks verify packaged CLI, updater and recovery guide. Windows worker generation/handoff is structurally tested only; no physical Windows self-upgrade, live service stop or real GitHub code installation is claimed.
