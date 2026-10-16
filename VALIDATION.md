# Engineering validation — 16 October 2026

## Executed checks

| Check | Result |
| --- | --- |
| `python -m pytest backend/tests -q` | **231 passed**, one upstream Starlette/httpx test-client deprecation warning |
| `python -m ruff check backend scripts` | Passed |
| `pnpm --dir frontend typecheck` | Passed, strict TypeScript |
| `pnpm --dir frontend build` | Passed, Vite production assets generated |
| Alembic upgrade of a populated SQLite `0001` database to `0002` | Passed; stored locations, shift history and accrued TOI preserved |
| Repeated migration and clean-checkout SQLite directory creation | Passed |
| Compose YAML parsing | Passed; PostgreSQL, backend and frontend services present |

Tests cover clock stoppages, period boundaries, corrections, shift statistics, every location transition, individual/forward/pair/mixed changes, goalkeeper changes, undo, recommendation learning and reversal, transactional rollback, idempotent receipts, concurrent writers, WebSocket ordering/reconnection, database restarts, permissions, provider validation/retries/cache, import and conservative refresh reconciliation. Ordinary tests mock all external HTTP.

The two-period domain scenario and imported-match integration scenario compare TOI with independently calculated expectations. The imported scenario exercises individual, forward-line and defensive-pair changes, penalty entry/return, stoppages, metadata refresh and persistence reload. Its independently expected elapsed playing time is 150 seconds.

## Live browser verification

The native FastAPI service and React operator application were started and exercised in the Codex browser:

- Created an independent demonstration match, edited a jersey number and saved the starting roster.
- Started/paused the clock; used two-tap substitution, full forward-line and defensive-pair changes.
- Moved a player from ice to penalty box, to bench, and back onto the ice.
- Pulled/restored a goalkeeper and independently added/removed an extra attacker.
- Confirmed and atomically undid a mixed centre/defender substitution.
- Viewed live statistics and an individual player's active shift history.
- Fetched the real Hokejový Zápis Game ID **966**, reviewed its complete roster, then explicitly confirmed the import.
- Confirmed 19 Milton Keynes Lightning players, 17 Solway IHC players, both official starting sixes, source status Completed and an independent local session.
- Used the imported forward-line and defensive-pair presets and penalty-box controls.
- Compared and applied a source refresh, then reloaded the browser. All 36 player TOI totals and all shift records were unchanged.

For that live refresh check, playing time remained **34,400 ms** and summed player TOI remained **411,486 ms**. SHA-256 comparisons of the per-player totals and complete shift lists matched before and after refresh/reload. Game version advanced from 7 to 8, as expected for the accepted refresh.

The live provider response places rosters at the response top level, while the supplied shortened example nests them within `gameInfo`. Both formats are tested. Game 966 currently returns Solway IHC rather than the Basingstoke opponent in the shortened example; the application imports actual source data.

## Responsive checks

Both teams stayed side by side. At **1920×1080**, the complete rosters and penalty areas fit within the viewport in the spacious layout, including the source-warning banner. At **1366×768** in compact mode, both penalty areas ended at approximately pixel 752 and there was no horizontal page overflow. Imported preset labels are shortened only on the live console; full names remain in setup and accessible labels.

## Limits of this validation

Docker is not installed on the local Windows host. The original native application and automated persistence/concurrency tests used SQLite. The later Linux container CI run recorded below now verifies Docker builds, nginx and a running PostgreSQL deployment.

Production identity integration, multi-worker pub/sub, formal penalty enforcement, authoritative strength-classified TOI, actual OCR-clock integration and direct vMix output remain documented extension points. The standalone manual clock, imported rosters, tracking, recommendations and persisted statistics work without continued access to the external roster API.

## Operator simplification and fullscreen validation

The live console now prioritizes jersey numbers and assigned roles, with both teams, grouped bench players and compact penalty boxes. Statistics, full history/corrections, source detail and setup remain separate views. Presets derive outgoing players from their own forward/defence category; arbitrary mixed changes remain explicitly selectable, including unequal outgoing/incoming counts. Suggestions are bound to the exact current selection and snapshot version, so older results cannot remain actionable during a new request.

| Check | Result |
| --- | --- |
| Frontend Vitest/Testing Library suite | **61 passed**: App, fullscreen-hook, manual-clock, UUID and action-recovery regressions |
| Strict TypeScript and Vite production build | Passed |
| Backend regression suite | **231 passed**; backend unchanged by this presentation update |
| Backend Ruff checks | Passed |

Automated coverage includes native entry, visible exit, Escape/browser-driven exit, denied/unavailable fullscreen, delayed native grants/rejections, pending actions, exact board DOM identity and selection preservation, two-touch substitutions using the first tap's game time, complete line/pair changes, arbitrary mixed changes, penalty movements, undo, stale clock warnings and pending/offline locks. Deferred recommendation responses verify that a prior forward group cannot be submitted against a newly selected defensive pair. Overtime preparation tests cover 5/20-minute periods, validation, cancellation, repeated overtime and pending controls; only Apply clock submits the existing clock action.

Configured special units remain available through a compact disclosure during group selection, including units already partly on ice. Regressions cover that preset action and disabled unavailable/pending units without duplicate player tiles.

### Actual browser checks

Used the running React/FastAPI application with the imported 36-player match. Native fullscreen entered successfully and removed navigation, match/configuration/documentation controls. Both the visible exit and Escape restored the standard layout while retaining the selected outgoing player. Before any movement tests, repeated layout transitions preserved match version **10**, elapsed playing time **34,400 ms**, and a SHA-256 digest of complete players, shifts, events and clock values: `00cd511965c21521d8348644c87b7b36da35be36a59457587ab5ca774ae78346`.

In native fullscreen, verified two-tap substitution, direct forward-line/pair changes, a mixed two-out/one-in change, penalty entry/return to bench, independent return to ice, and immediate undo. Rechecked presets after selecting one individual forward or an unrelated forward group: each preset changed only its own category. All test movements were made with the clock paused; the original on-ice lineup was restored, elapsed playing time remained **34,400 ms**, and summed TOI remained **411,486 ms**. Prepared a 20-minute overtime in the fullscreen clock form and closed it without applying; the authoritative period stayed unchanged.

Measured both standard and native operator layouts with actual browser viewport overrides:

| Viewport | Result |
| --- | --- |
| 1920×1080 | Both complete teams/benches/penalty areas fit; document and operator root measured 1920×1080; no horizontal or vertical scrolling, including a selected-player context |
| 2560×1440 | Both teams and penalties fit; document/root matched viewport dimensions |
| 1366×768 | Both teams/benches/penalties fit; minimum player tile 86×56 px |
| 1024×768 landscape | Both teams/benches/penalties fit; minimum player tile 58×56 px |

At 1920×1080 native fullscreen, player tiles measured at least **128×76 px**, and both penalty areas ended at pixel **1068**. Screenshots were visually inspected; `artifacts/operator-fullscreen.jpg` records the number-first fullscreen console with an outgoing selection and suggestions highlighted on the bench.

The browser on this host allowed native fullscreen. Rejection/unavailable paths and browser-driven exit events were exercised through automated browser-API doubles. Touch interactions were simulated with Testing Library pointer events; physical touchscreen hardware was not available. Future website chrome hiding uses the documented `lightning-toi:operator-layout` contract and requires the host route to adopt that contract.

### Earlier iPad and quieter-view revision

The later operator request supersedes the always-visible bench presentation above. The idle view now contains on-ice players and compact team actions, with no bench tiles or empty penalty footer. Selecting one outgoing player reveals up to three suggested replacement tiles. **All replacements** switches to the complete grouped bench without duplicate player tiles; **Bench** provides independent bench movements. Line, pair and goalkeeper controls are in **Lines & pairs**. Cancellation and successful movements close the bench again. Expanded alternatives and the current selection survive fullscreen entry/exit.

The final frontend suite has **61 passing tests**; strict TypeScript and the production build pass. Browser verification confirmed zero idle bench tiles, three replacement tiles after a player selection, full alternatives without duplicate IDs, a successful suggested two-tap substitution and immediate undo. At **1024×768 landscape**, both teams and contextual replacements fit without page/root overflow; suggested tiles measured 110 px wide. At **768×1024 portrait**, teams stack, both idle panels fit within the viewport, and player tiles measured at least 115 px wide. The selected tile retains a gold background during hover, including touch browsers that retain hover styles.

Also exercised the running application through the computer's actual HTTP LAN origin rather than localhost: the WebSocket connected, recommendations loaded, and a substitution plus undo completed on the paused demonstration match. Secure request IDs now use `crypto.getRandomValues()` when secure-context-only `crypto.randomUUID()` is unavailable. Tests verify UUID v4/variant bits, secure fallback uniqueness, identical retry IDs/bodies and release of the pending lock after a client-side failure. No predictable ID fallback is used. This host does not provide physical iPad/Safari hardware; viewport and HTTP-LAN checks used the available browser.

### Contextual bench refinement

The latest refinement provides explicit **Show full bench**, **Add player**, **Move to bench**, **Send to penalty box**, **Return to ice** and **Return to bench** actions. The full bench contains only eligible players, grouped by existing line/pair/goalkeeper assignments. Its own scrolling area keeps both current lineups visible. Occupied penalty tiles sit above the expansion and remain independently actionable. Ranked suggestions still come from the existing backend endpoint; the frontend only filters current eligibility and displays the first returned candidates.

The current frontend suite passes **73 tests**, including **38 operator regressions**. It verifies suggested two-touch substitutions at the first tap's playing time; Add player with no outgoing player; successful closure even before a new snapshot arrives; retained context after failed actions/undo; unchanged expansion across clock refreshes and fullscreen; cancellation followed by reselecting the same player; line/pair/mixed groups; penalty returns; and short-handed/empty-net lineups. Strict TypeScript and the production build pass. No backend or recommendation-engine source changed in this refinement.

Actual browser checks at **1024×768 landscape** measured zero idle bench tiles and both teams visible. Opening the 13-player imported bench kept document/root dimensions at 1024×768, with bench tiles at least **58 px** wide. At **768×1024 portrait**, both teams now remain side by side; the expanded bench had a 319 px client height with 489 px scroll content, and bench tiles measured at least **91 px** wide. Document/root dimensions stayed 768×1024. Native fullscreen, visible exit and Escape preserved both the outgoing selection and the expanded bench; Add player expansion also survived mode changes.

Before movement testing, expanding/collapsing panels and changing display modes preserved imported-match version **30**, elapsed playing time **34,400 ms**, summed TOI **411,486 ms**, and SHA-256 digest `45789d9b03324517704317af8777540637e1c1d86f5fefbc23fa34fe3e7bb4c4` of players, shifts, events and clock values.

On the separate paused training match, exercised Add player without an outgoing selection, five/six/seven-player lineups, both penalty returns without bench expansion, Move to bench, a suggested two-tap substitution, a complete forward-line preset, automatic panel closure, and Undo. All test movements were undone to restore the starting lineup. At 1024×768, a seven-player lineup retained touch targets at least **62 px** wide and its expanded bench remained within the viewport. Physical iPad/Safari hardware remains unavailable; these were actual browser viewport checks. Local screenshots are saved under the ignored `artifacts/` directory.

## Linux Portainer stack validation

[compose.portainer.yaml](compose.portainer.yaml) is an image-only stack for Linux Docker Standalone. [PORTAINER.md](PORTAINER.md) documents building both local images in Portainer, deploying through its Web editor, required secrets/origins, iPad access, updates and backups. Database and backend ports are private, PostgreSQL uses a named volume, and nginx refreshes Docker service DNS after backend recreation.

Native checks after the deployment changes: **253 backend tests passed**, including 22 PostgreSQL configuration cases; Ruff passed; both Compose files and the workflow parsed successfully. Discrete database credentials round-trip reserved URL characters, spaces and Unicode through SQLAlchemy, while explicit database URLs and native SQLite defaults remain supported.

The [Linux container smoke test](https://github.com/monty23monty/MKLTV-TOI-V1/actions/runs/38946708889) passed for runtime commit `2dffac6e2687f7281936cb2519fbc443f2b9137d` on GitHub's Ubuntu runner. It verified:

- Both production images build from the checked-in Dockerfiles and locked dependencies, including the frontend TypeScript/Vite build.
- Docker Compose validates the image-only stack and all three services become healthy with PostgreSQL 17.
- A database password containing `@`, `:`, `/`, `?`, `#`, `%` and `$` connects successfully.
- nginx serves HTML and built JavaScript, and forwards health/OpenAPI/API paths with query strings.
- Unauthenticated HTTP and WebSockets are rejected; authenticated API, recommendations and WebSocket snapshots work through nginx.
- A paused substitution changes locations and broadcasts the matching version; Undo restores the lineup without accruing playing time or TOI, while retaining event history.
- Removing and recreating all containers without deleting the PostgreSQL volume preserves the complete game fingerprint; HTTP and WebSockets resume correctly afterward.

Only disposable CI containers/volumes were removed. The workflow publishes no images, so the documented local-image build step is still required on the user's Docker endpoint. The actual Portainer GUI/server was not connected in this session; the deployment instructions were checked against official Portainer documentation. ARM64 execution and physical iPad Safari remain untested.

### Portainer 2.45.0 URL builder correction

After a user reported the default-Dockerfile error with the correct path field, the exact Portainer 2.45.0 source showed that its URL-build query serializer inserts the remote Git URL without escaping `#` before the `dockerfile` parameter. A Node URL-parsing reproduction confirmed that a literal Git fragment removes that parameter from the server-bound query; both a plain `.git` URL and a `%23`-encoded fragment preserve it. `git ls-remote --symref origin HEAD` confirmed `codex/ipad-operator` is the repository's default branch. The guide now uses the plain URL and explains the affected-version workaround. This was a source-level/request-parsing check; a new build on the user's Portainer host has not been observed. No application or image runtime files changed.

## Earlier external service interface design validation

[SERVICE_INTERFACE.md](SERVICE_INTERFACE.md) documents current HTTP input and outbound v1 WebSockets, then specifies an unimplemented v2 duplex service protocol for authoritative clock input, individual player TOI, on-ice lineups and a durable recorded-change journal. The design includes explicit clock ownership, replay barriers/checkpoints, service authorization, flow control, deployment and measurable latency/soak acceptance criteria.

The offline [contract checker](scripts/validate_service_interface.py) passed the Draft 2020-12 schema, all **41 accepted and 39 rejected examples**, direction separation and UUID format assertions. All **12 embedded v1 payload definitions** match the existing Pydantic source, and examples for all six existing action types pass those models. Additional CLI checks reject wrong directions, remote schema references, duplicate JSON keys and non-JSON numeric constants. Ruff passed for the checker. Its optional JSON Schema dependency was installed only under ignored tooling artifacts; production dependencies are unchanged.

The existing API regression module passed **15 tests** (one existing Starlette/httpx deprecation warning). No application source, tracking rules, UI or image runtime changed in this documentation/design task. No v2 endpoint, durability guarantee, latency benchmark or soak result is claimed as implemented or measured; those acceptance checks remain work for the future runtime implementation. The user's Portainer frontend restart issue was not verified as resolved during the design task.

## V2 runtime and release validation

Release 2.0.1 implements the previously documented protocol. The earlier design-only status above is retained as a historical record and is superseded by this section.

Native validation passed **338 backend tests**, including storage/journal, clock fencing, service authorization, real ASGI duplex sessions, replay barriers, strict frame validation and bounded shared admission. The last WebSocket overload refinement also passed the 18 coordinator/v1 API regressions. **79 frontend tests**, strict TypeScript and the production Vite build passed, including operator clock-source selection, lost-response retries and stale/disconnected interpolation. Ruff passed. The contract checker passed all **41 positive / 39 negative examples**, 12 matching v1 payload definitions and six command types. One existing Starlette/httpx deprecation warning remains.

The [Linux PostgreSQL/nginx container run](https://github.com/monty23monty/MKLTV-TOI-V1/actions/runs/38975191444) passed for implementation commit `b93f4aeaef4d654459a0943b40d4912f0550cb64`. It built both final images, verified the original operator workflow, and exercised the v2 handshake, authenticated reads, command ACK linked to its journal record, duplicate retry, penalty transition, replay, explicit clock handover, manual fencing and undo. Removing/recreating the containers with the same PostgreSQL volume preserved both the v1 match fingerprint and v2 journal checkpoint. The release workflow repeats these checks on the tagged revision before publishing artifacts.

### Short local network measurement

[scripts/benchmark_service_network.py](scripts/benchmark_service_network.py) starts an isolated backend and temporary SQLite database. The measurement used Windows 11, Python 3.12.14, real uvicorn loopback TCP, 40 rostered players, 200 preloaded movements, **20 v2 service sockets plus two v1 operator sockets**, and 30 mutations over 9.707 seconds: ten running external-clock observations and 20 movements.

| Measurement | p50 | p95 | p99 | Maximum |
| --- | --- | --- | --- | --- |
| Command send → committed ACK | 39.221 ms | 59.484 ms | 71.879 ms | 75.204 ms |
| Command send → slowest of all 20 record recipients | 50.202 ms | 70.215 ms | 83.887 ms | 87.728 ms |
| Fresh connection → snapshot/ready | 33.851 ms | 50.307 ms | 51.289 ms | 51.534 ms |
| Consecutive live tick interval | 220.994 ms | 245.522 ms | 252.973 ms | 253.129 ms |

Delivered **600/600 expected records**, with zero frame/journal sequence gaps; every consumer received complete 40-player timing samples. The maximum tick interval slightly exceeded the provisional 250 ms upper bound. These results predate the final bounded-admission refinement and are a short local sample, not proof of the three-hour PostgreSQL/nginx/LAN reference target. TLS, actual iPad hardware and LAN latency were not measured. The ignored JSON report is `artifacts/service-network-benchmark.json`.

### Storage growth measurement

[scripts/benchmark_service_store.py](scripts/benchmark_service_store.py) preloaded 300 valid movements, then applied 300 logically 1 Hz observations at accelerated speed on isolated SQLite. Per-observation storage latency was p50 **11.546 ms**, p95 **12.888 ms**, p99 **16.550 ms**, maximum **26.340 ms**. Observations added no domain history events or shifts; the history payload stayed at **141,522 bytes**. Compact receipts stayed at **240 bytes** each rather than retaining a full growing match response. The 300 clock journal records added **5,658,238 JSON bytes**; allocated database size increased by **6,963,200 bytes**. This is storage-only validation, without network fanout or real-time pacing; it does not establish long-term retention capacity. The ignored report is `artifacts/service-store-benchmark.json`.

### Update architecture checks and limits

Both images carry a matching version and full source revision. CI exercises PostgreSQL migration/startup, nginx routing and persistence before publication; the release pipeline additionally saves/reloads the image bundle and verifies embedded revisions. `release-manifest.json` records actual registry publication and anonymous pull outcomes. [UPDATING.md](UPDATING.md) documents backup, retained `toi-v1_postgres_data`, version-tag updates, archive fallback, compatible rollback and offline generation rotation after restore.

The initial `v2.0.0` tag failed before image publication because the release job's integration-test operator credential leaked into two demo-mode unit tests. The unit-test step now explicitly clears operator/service credentials and disables streaming unless a test enables it. The pushed tag was retained; `v2.0.1` is the first installable release.

The [v2.0.1 release workflow](https://github.com/monty23monty/MKLTV-TOI-V1/actions/runs/38975955234) then passed on exact revision `1a76b9a10d03b12c7249de0aca08c48c8970f22e`: **338 backend / 79 frontend tests**, build, Ruff, contract validation, PostgreSQL/nginx v1/v2 smoke, and persistence after container recreation with a different server epoch. The short container smoke's maximum observed round trip was **76.9 ms**, without reference-load conditions. Both images were pushed and pulled anonymously. The [public release](https://github.com/monty23monty/MKLTV-TOI-V1/releases/tag/v2.0.1) and every asset were independently downloaded without credentials; all SHA-256 checks matched. The 105,778,068-byte archive has SHA-256 `3b746e0c60a6e3859d03d65e9f0b3fdf0ffa80ce8d87cdef95fa0d6b72202b86`. Its two final runtime image configurations match the manifest IDs/version/revision; the frontend command is `nginx -g daemon off;`, not the intermediate Node build stage.

The user's Portainer host was not accessed or upgraded. Linux amd64 container validation does not imply ARM64 support; the first bundle is amd64 only. The sustained three-hour reference soak, production monitoring and physical iPad Safari validation remain deployment checks. No unattended production update is configured.
