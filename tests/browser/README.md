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
