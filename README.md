# Lightning TV · Live Time on Ice

A working standalone ice hockey operator dashboard for tracking both teams, substitutions, physical penalty-box locations and live time on ice. The dark, gold-accented React interface is designed for touch operation. FastAPI owns all player and timing state, with PostgreSQL persistence in Docker and SQLite for quick native development.

The demonstration uses **fictional** Milton Keynes Storm and Northbank Wolves rosters: 20 players per team, four forward lines, three defensive pairs and two goalkeepers. No third-party services or accounts are required for the demo. An optional Hokejový Zápis import loads real match metadata, complete rosters, official starting players and generated lines/pairs from a numeric Game ID.

## Start with Docker

Install Docker with Compose, then run from the repository root.

PowerShell:

```powershell
Copy-Item .env.example .env
docker compose up --build -d
docker compose logs -f backend
```

Ubuntu:

```bash
cp .env.example .env
docker compose up --build -d
docker compose logs -f backend
```

Open [the operator dashboard](http://localhost:8080), [API documentation](http://localhost:8000/docs) and [health status](http://localhost:8000/health). The first launch applies migrations and creates the demonstration game automatically. Subsequent launches resume stored games. PostgreSQL data lives in the `postgres_data` named volume. `docker compose down` stops services and preserves games; `docker compose down -v` deliberately removes that database volume.

## Native development

Requirements: Python 3.12 or newer, Node.js 22 or newer and pnpm 11.25.0 (pinned in `frontend/package.json`). Run commands from the repository root. The default database is `data/toi.db`; Docker is not needed for this development path. Backend environment variables come from the shell; copying `.env` alone does not load it into a native Python process.

PowerShell, terminal 1:

```powershell
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r backend/requirements.lock.txt
& .\scripts\dev-backend.ps1
```

PowerShell, terminal 2:

```powershell
corepack enable
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend dev
```

Ubuntu, terminal 1:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock.txt
sh scripts/dev-backend.sh
```

Ubuntu, terminal 2:

```bash
corepack enable
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend dev
```

Open [localhost:5173](http://localhost:5173). Vite proxies `/api` and WebSockets to port 8000. To use PostgreSQL natively, set `TOI_DATABASE_URL` to a `postgresql+psycopg://user:password@host/database` URL before starting the backend. Apply schema upgrades explicitly with `python -m alembic -c backend/alembic.ini upgrade head` using your virtual environment's Python. Startup also applies pending migrations.

## Use on an iPad

Use landscape orientation for the two-team operating surface. Portrait orientation stacks the teams to retain comfortable touch targets. During normal play the bench stays hidden; selecting an outgoing player reveals suggested replacements, with the full bench available when needed.

Keep the server computer and iPad on the same network. With native development running, open `http://<server-LAN-IP>:5173` in Safari; with Docker use `http://<server-LAN-IP>:8080`. Replace the placeholder with the server computer's local network address. `localhost` on the iPad refers to the iPad itself. The development script already listens on the network; API and WebSocket traffic use the frontend's same-origin proxy.

Operator Full Screen uses native fullscreen where Safari permits it and explicitly falls back to the focus layout otherwise. Safari on iPadOS 16.4 introduced the unprefixed Fullscreen API ([WebKit release notes](https://webkit.org/blog/13966/webkit-features-in-safari-16-4/)). The application also generates secure request identifiers on HTTP LAN origins where `crypto.randomUUID()` is unavailable. For shared deployment, follow the existing HTTPS/authentication and origin configuration guidance in [INTEGRATION.md](INTEGRATION.md).

## Operator workflow

1. Load the demo or create a match. Open match setup to edit names, jersey numbers, positions, eligibility, starting locations and line presets. Add players or import JSON before tracking; unlisted players remain in the roster, so mark unused players unavailable on the bench.
2. Start the manual clock at puck drop and pause it at stoppages. A stopped clock contributes no TOI. Clock controls also set countdown, period and period lengths.
3. Tap an on-ice jersey number, then one of the suggested replacement numbers that appears. **All replacements** opens the complete bench when a different player is needed. Cancel or a successful change returns to the quiet view.
4. Open **Lines & pairs** for forward/defence selection, named presets and goalkeeper controls. Use **Group select** for arbitrary multi-player changes and confirm once for an atomic operation. Configured special units remain available during group selection.
5. Use **Bench** when an independent bench-player movement is needed. Selecting a player reveals ice, bench and penalty-box moves. Occupied penalty boxes stay visible; players never return automatically.
6. Use Undo for the latest movement. The separate history view provides auditable movement corrections and rebuilds shifts and recommendation learning.
7. Open Statistics for TOI, completed shifts, averages, per-period totals and individual shift history. Refreshing or reopening the match resumes its persisted state.

The UI shows pending changes, connection status and non-blocking lineup warnings. It disables new movement submissions while disconnected or awaiting a result, retries the same request ID after a lost response and resynchronizes on version conflicts. It does not queue speculative offline player changes.

### Operator Full Screen

Choose **Operator Full Screen** from live tracking for a dedicated two-team control surface. The navigation, match picker, setup, statistics, documentation and event timeline are hidden. Jersey numbers, assigned positions, contextual replacements, compact bench/line access, occupied penalty boxes, period/clock, Undo and an exit control remain available. Standard and fullscreen layouts share the current match, selections and live connection.

Use **Exit Full Screen**, Escape or the browser's fullscreen exit to return. If native fullscreen is unavailable or declined, an explicitly labeled focus layout provides the same simplified controls within the browser window. Exit focus layout returns to the normal interface. Compact/spacious preference remains separate from fullscreen state. See [INTEGRATION.md](INTEGRATION.md#operator-layout-and-browser-fullscreen) for the host website's navigation/sidebar layout contract.

Tap the clock to set the period/countdown or advance a period. At the final configured period, choose the overtime duration and **Prepare overtime**, then **Apply clock**. Preparation alone changes no tracking state. These essential manual controls remain available in fullscreen.

## Import a Hokejový Zápis match

1. Choose **Import match**, enter a positive numeric Game ID (for example, `966`) and load the preview. The backend fetches the roster; the browser does not contact the external provider.
2. Review both teams, player names/numbers, starting players, forward lines, defensive pairs and any source-data warnings. Structured roster codes determine roles; official `startingSix` flags determine the proposed on-ice players.
3. Choose the official starting lineup, or establish the current lineup manually if joining a match already in progress. Set the coverage period and remaining time for a partial-match session. No earlier TOI is invented.
4. Confirm import, review any final setup adjustments, then start the local game clock. Importing a completed source match is allowed for training or reconstruction; source status and local tracking status remain separate.

Importing the same provider Game ID reopens its existing local session. It never resets locations, shifts or learning. Once imported, tracking continues without internet access. **Refresh source roster** fetches a reviewable metadata diff; apply it explicitly. Existing physical locations, shifts, clock and event history remain authoritative. Removed source players are retained and flagged; new players enter on the bench. Local edits and line presets are preserved unless the operator explicitly restores the imported lines.

The provider defaults in [.env.example](.env.example) work without extra runtime dependencies. A preview cache lasts 60 seconds by default; explicit refresh bypasses it. See [INTEGRATION.md](INTEGRATION.md) for identifier mapping, roster-code rules, source snapshots and reconciliation policies.

If import fails, verify the numeric Game ID and backend network access, then inspect the displayed warning/error. A normal preview may recover the last cached response with a prominent stale-data warning and its age; review that warning before confirming. Forced source refresh reports failure instead of silently applying stale data, and the existing local game remains usable. Duplicate identities require reconciliation rather than guessing a player match. If the preview changed before confirmation, reload it and review the new data. Do not repeatedly refresh a rate-limited provider.

## Commands

Use the virtual environment's `python` executable for backend commands (`.venv\Scripts\python.exe` on Windows or `.venv/bin/python` on Ubuntu).

```bash
python -m pytest backend/tests -q
python -m ruff check backend scripts
pnpm --dir frontend test
pnpm --dir frontend typecheck
pnpm --dir frontend build
python scripts/seed_demo.py --name "Training match"
```

`seed_demo.py` requires a running API and creates another fictional match without overwriting existing games. If authentication is configured, set `TOI_AUTH_TOKEN` to an authorized token in the script's environment.

### Reproduce the match scenario

```bash
python -m pytest backend/tests/test_domain.py -k realistic_two_period -v
```

This accelerated two-period scenario uses a 120-second first period and 40 seconds of play in period two. It includes a forward-line change after 30 seconds, a 60-second wall-time stoppage that contributes zero TOI, a penalty-box entry/return, a goalkeeper pull, correction of the mistaken incoming player, an intermission and the goalkeeper's return. The final clock is paused, so waiting longer must not increase any total.

Expected values are calculated directly from the fixture's movement intervals, independently of the statistics implementation:

| Fixture player | Playing-time intervals on ice (seconds) | Expected TOI |
| --- | --- | --- |
| `h1` | 0–30 and 110–140 | 60 seconds |
| `h2`, `h3` | 0–30 | 30 seconds each |
| `h4`, `h5` | 0–160 | 160 seconds each |
| `h6` (goalkeeper) | 0–110 and 140–160 | 130 seconds: 110 in P1, 20 in P2 |
| `h7` | 30–60 and 90–160 | 100 seconds |
| `h8`, `h9` | 30–160 | 130 seconds each |
| `h13` (mistaken replacement) | Removed by correction | 0 seconds |

The test asserts 160 seconds of accumulated playing time, these totals, and consistency between per-period and overall TOI. The broader test suite also covers domain, recommendation, API, persistence, concurrency, idempotency and live resynchronization behavior. Executed results and environment limits are recorded in `VALIDATION.md` after validation.

For the imported-match lifecycle, run `python -m pytest backend/tests/test_hokejovy_integration.py -q`. Its fixture-backed scenario imports both real source rosters, performs individual/forward-line/defensive-pair changes and penalty movements over 150 seconds, pauses, refreshes metadata and reloads persistence. Independent expectations include 150 seconds for the starting goalkeeper, 70 seconds for a forward who spends 20 seconds in the penalty box, and 90/60 seconds for the outgoing/incoming defensive pairs. Ordinary tests make no live provider requests.

## Configuration and integration

See [.env.example](.env.example) for all deployment settings. Empty authentication settings enable local demonstration access. `TOI_AUTH_TOKEN` protects the prototype with a shared bearer token; `TOI_OPERATOR_TOKENS` supports named operators assigned to specific team IDs. Enter a token through the operator interface when required. Use TLS and connect existing website identity for a shared deployment.

See [ARCHITECTURE.md](ARCHITECTURE.md) for timing, persistence and concurrency decisions; [API.md](API.md) for commands and live snapshots; and [INTEGRATION.md](INTEGRATION.md) for website mounting, roster imports, scoreboard observations and graphics consumers.

Run a single backend worker until the process-local WebSocket notification bus is replaced with shared pub/sub. Formal penalty enforcement and strength-classified TOI, seasonal totals, direct vMix output and real OCR/authentication integrations remain future work. The prototype records physical locations and reports even-strength/power-play/penalty-kill statistics as unavailable rather than inferring rules from the penalty box.

The current implementation was tested natively with SQLite. Docker is not installed in the implementation environment, so the Compose YAML was parsed but container builds and a live PostgreSQL deployment could not be exercised there. Run the Docker smoke check on a Docker-enabled host before operational use.
