# Dashboard interaction regressions

Runs the real dashboard JavaScript and layout in headless Chromium at desktop and
mobile sizes. API responses and SSE events are **test-only fixtures**, intercepted
inside the test browser. No model inference, credentials, code generation, or tool
execution is performed. Production dashboard code does not contain these fixtures.

Requires Node 22.17+ and a local Nexus server in **live** mode:

```sh
python -m nexus serve --port 8787 --quiet
# in another terminal
cd tests/browser
npm ci
npm test
```

Default test URL is `http://127.0.0.1:8787`; override with `NEXUS_TEST_URL`.
On Linux the npm-packaged Chromium requires the usual browser shared libraries
(including libnspr4 and libnss3). Alternatively set `CHROMIUM_EXECUTABLE_PATH` to a
locally installed Chromium/Chrome executable (also the option for Windows/macOS).
The npm browser dependency is development-only and does not ship with Nexus.

Coverage: approval visibility/clicks, failed-request retry, acknowledgement races,
side-card sizing, pipeline selection persistence, recall errors, stop errors,
clear-during-run protection, duplicate SSE handling, live-only task gating, and
refresh recovery at mobile width. Backend HTTP/SSE and permission-gate regression
coverage lives in `tests/test_server.py` and `tests/test_tools.py`.

Workbench coverage additionally checks provider/metrics buttons, persisted model
and write-folder policy, history/plan loading, changed-file reads, queue controls,
error explanations, model capability controls, cancellation before model deletion,
Bengali labels, saved focus layout, and command-palette keyboard behavior on mobile.

Studio coverage also checks safe conversation rendering/immutable branching,
mobile visual graph construction and version saves, dry-run controls, discoverable
live child approvals, clarification answers, actionable runtime failures, and
closing Studio when a normal approval arrives. All model/API fixtures are test-only;
production Studio pages read their authenticated live services.

Hardware/live-process coverage checks removal of setup instructions, measured
CPU/RAM/GPU rendering, missing GPU values, stale-reading clearing, stdout/stderr
before completion, stop acknowledgement/failure retry, bounded expandable tool
output, applied diffs, event deduplication, tool filters and hostile-output escaping.
Actual subprocess/process-group and HTTP/SSE cancellation tests are separate in
`tests/test_live_process.py` and `tests/test_server.py`.


## Environment-control panels (Task 12)

`node --test environment.test.mjs` loads the actual Computer/Assistant panel scripts
in Chromium with **in-page fake APIs**. It needs no Nexus server, tokens, external
network, native apps or devices. It checks queued-vs-completed wording, the GUI
approval route, exact one-use legacy payloads and error handling, plus the assistant
panel at phone width. Install this directory's development dependencies with
`npm ci` first. It is not native desktop or physical-device acceptance.

The separate `computer.test.mjs` is an **optional live dashboard** suite: it performs
real scratch file writes/trash/restore and changes Studio settings. Run only against
an isolated workspace whose operator has explicitly enabled `computer.files` with
`roots: [tests/browser]`, and a matching local tests/browser directory. Set
`NEXUS_GUI_TEST_TOKEN` for that test server. Reusable WRITE/RESTORE phrases no longer
suffice; the suite requests the same one-use reviews as the UI. Do not enable file
access in a production workspace merely to run a browser test. The general
`npm test` entry point above runs only `dashboard.test.mjs`, not every optional suite.


## Shared HUD / PWA (Task 13)

```sh
npm ci --no-audit --no-fund
node --test presence.test.mjs environment.test.mjs
```

These suites need **no running Nexus server**. `presence.test.mjs` loads the actual
HUD scripts and complete dashboard shell with intercepted fake HTTP/SSE/audio.
It covers English/Bangla at 360/1200px, exact reviews and device scopes, fragmented
UTF-8 SSE, reconnect/OFF/revocation, microphone permission races/errors, playback
opt-in and once-claim behavior, same-instance World reparent/late-import adapter
races, and static-only service-worker caching/generic push navigation. The World
race test substitutes only the dynamically loaded map module to avoid WebGL/network;
the shell, navigation and panel code are real. It is not native-device or PWA
installability acceptance. API/token/storage/service/Telegram mocks are in
`tests/test_presence*.py` and `tests/test_telegram_voice.py`.

Use `CHROMIUM_EXECUTABLE_PATH` for an existing local browser if the npm-packaged
Chromium cannot find its Linux shared libraries. The development sandbox ran these tests with
`LD_LIBRARY_PATH=/tmp/al2023/lib` after expanding the npm package's bundled
`bin/al2023.tar.br` libraries into `/tmp`; that temporary path is not portable or
persisted with the repository.

Normally install the platform's standard Chromium dependencies instead; no library
bootstrap or browser download is part of the application. Do not run the optional
live `computer.test.mjs` against a production workspace as part of this offline check.
