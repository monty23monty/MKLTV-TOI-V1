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
