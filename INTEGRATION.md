# Integrating with Lightning TV

This repository defines its own TOI interfaces and includes a Hokejový Zápis adapter for the match-information endpoint specified below. It does not assume that an existing Lightning TV service has a particular endpoint, identity format or OCR payload. Agree those contracts before connecting other providers.

## Mount the operator interface

For an existing React application, mount the operator component in an internal route and include its stylesheet. Keep API calls and live synchronization in `frontend/src/useGame.ts`; pass or adapt the website's authenticated request mechanism there. In Next.js, the operator and hook must be rendered as client components because they use browser APIs and WebSockets. Set the containing route to use the available viewport rather than a constrained article layout. No iframe is required.

Alternatively, build the frontend and serve its static output beneath the website's operator route. Set Vite's `base` if hosted under a path prefix, and configure the hosting server to return that route's `index.html` for client paths. Proxy `/api/v1/` and WebSocket upgrade requests to the separately deployed backend. `VITE_API_BASE_URL` selects a separate API origin at build time. If its origin differs from the website, explicitly configure `TOI_CORS_ORIGINS` and allow the website origin for WebSockets.

A non-React website can keep the frontend as a route served by its reverse proxy, or implement its own controls against the versioned API. It does not need to adopt FastAPI internally, nor use the prototype's React code to consume statistics.

## Operator layout and browser fullscreen

The live operator view and **Operator Full Screen** share the same mounted React application, `useGame` connection, selection and pending action. Fullscreen is a layout change, never a new tracking session. The browser request targets `#lightning-toi-operator`, so a successful native fullscreen request displays that component rather than the surrounding website.

`frontend/src/useOperatorMode.ts` listens to `fullscreenchange` and Escape. Browser-driven exit returns the application to its standard layout. When the API is unavailable or rejects a request, the application explicitly labels its distraction-free **focus layout**; it does not claim that browser fullscreen is active. The visible exit control also works in that fallback. Active fullscreen is not restored automatically on refresh, because a browser requires a fresh user gesture.

For a shared website route, use the explicit layout event rather than selectors that reach into unrelated parent navigation:

```typescript
type OperatorLayout = {
  active: boolean;
  mode: 'standard' | 'browser' | 'focus';
  browserFullscreen: boolean;
  elementId: 'lightning-toi-operator';
};

useEffect(() => {
  const handleLayout = (event: Event) => {
    const layout = (event as CustomEvent<OperatorLayout>).detail;
    if (layout.elementId === 'lightning-toi-operator') {
      setHideWebsiteChrome(layout.active);
    }
  };
  window.addEventListener('lightning-toi:operator-layout', handleLayout);
  return () => window.removeEventListener('lightning-toi:operator-layout', handleLayout);
}, []);

// In the host route's own layout:
// <WebsiteLayout showNavigation={!hideWebsiteChrome} showSidebar={!hideWebsiteChrome}>
//   <OperatorApp />
// </WebsiteLayout>
```

Install the host listener before the operator mounts, and reset the host's chrome state when leaving the route. The event is also emitted when the operator returns to standard mode or unmounts. A non-React host can listen for the same event and update its own layout state. Independent deployments need no host listener. Configure the route/container to allow the full viewport and permit the standard Fullscreen API where hosting policy permits it; the focus fallback remains usable when policy prevents native fullscreen.

Normal live controls show jersey numbers and assigned positions. Benches stay hidden until requested; selecting an outgoing player reveals the existing engine's ranked suggestions, with eligible alternatives accessible through **Show full bench**. **Add player** uses the existing move-to-ice action without an outgoing player. Occupied penalty boxes remain visible above expansions. Successful local operations close the contextual panels; failures and ordinary snapshot/clock refreshes preserve them. Names, complete statistics, source-review details and the auditable event timeline remain available in their separate views. The presentation changes do not affect API contracts, roster IDs, shift records, recommendation ranking or TOI calculation.

## Game, team and player ownership

Internal game, team and player IDs are stable opaque identifiers, never jersey numbers. `external_id` maps each entity to an existing platform identifier. Match rosters carry game-specific team membership, number, position and eligibility. The same real player can therefore retain their external identity across games with different numbers.

The prototype persists a match snapshot and rebuildable relational projections. Keep the website's games/players service as the master for identity and scheduling; import a match snapshot before tracking, then let TOI own locations, shifts, operator events and corrections. Do not allow an asynchronous roster refresh to overwrite an active game's lineup. Decide how late roster amendments are approved and audited, and map them to setup changes or explicit movement/correction commands.

`RosterProvider.load_game(game_id)` in `backend/toi/adapters.py` describes the original canonical-state boundary; `DemoRosterProvider` supplies the fictional fixture. The Hokejový Zápis integration adds preview/normalization before transactional creation and refresh. Other external providers should produce the same normalized match/roster DTO rather than insert raw source JSON into the shift engine. Validate unique IDs and jersey numbers, retain source provenance, and establish explicit initial locations. Starting shifts are reconstructed by the domain, so imports should not manufacture open-shift rows.

## Hokejový Zápis source

The backend constructs this read-only endpoint from configuration and a positive numeric Game ID:

```text
https://api.hokejovyzapis.cz/v1/d07ssvctxm/matches/{Game_ID}/game-info.json
```

For example, Game ID `966` requests `/matches/966/game-info.json`. Only match/roster information is read. This endpoint does not supply the authoritative local clock, physical locations, shifts, penalties or TOI, and the application never writes to it.

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOKEJOVY_ZAPIS_BASE_URL` | `https://api.hokejovyzapis.cz` | Trusted provider origin |
| `HOKEJOVY_ZAPIS_API_PREFIX` | `/v1/d07ssvctxm` | Configurable API path prefix |
| `HOKEJOVY_ZAPIS_TIMEOUT_SECONDS` | `10` | Bounded backend request timeout |
| `HOKEJOVY_ZAPIS_CACHE_TTL_SECONDS` | `60` | Preview-cache lifetime |
| `HOKEJOVY_ZAPIS_ALLOWED_HOSTS` | `api.hokejovyzapis.cz` | Explicit trusted HTTPS host allowlist |

These variables are passed to the backend by Compose. Native development reads them from the process environment. Operator input is a Game ID, never an arbitrary URL. The configured base must be an allowlisted HTTPS origin on port 443, without credentials, query or path. Change the trusted host allowlist deliberately if moving the integration behind an approved proxy. Prefix validation, fixed path construction and disabled redirects prevent changing the destination through the import form. Responses are capped at 2 MiB; at most three attempts use short exponential backoff for transient failures. Explicit refresh bypasses the preview cache. A successful imported game is persisted independently of this cache, so loss of provider connectivity does not interrupt tracking.

The in-process preview cache retains up to 64 match entries. A normal preview uses an unexpired cached result; if an expired entry cannot be refreshed, the response explicitly marks `cache.stale` and includes its age and `cache_error`. A forced refresh reports failure and may include a separately labeled `cached_preview` for recovery. It never presents stale source data as a successful fresh refresh.

### Data and identity mapping

`gameInfo.teamInfo.home` maps to the home team; `visitor` maps to away. The adapter processes both complete rosters, supporting the observed top-level `roster.home`/`.visitor` shape and the supplied `gameInfo.roster` variant. It imports team IDs/names/shortcuts, source match status, stadium, coaches, referees, results and extra-time/shootout indicators when present. Source completion status does not prevent a local tracking session.

| Source field | Application meaning |
| --- | --- |
| Game ID | Provider-namespaced external game identity; deterministic local imported game |
| Team `id` | Provider team reference, retaining its exact source value |
| `playerId` | Persistent external player identity, reusable across matches |
| `matchPlayerId` | External identity of this game's roster entry |
| `jersey` | Match-specific display number |
| Object key / `roster` | Original lineup slot/code, retained as text |
| `position` | Raw broad position, retained for review |
| Parsed code role | Assigned role used by the operator UI and initial recommendation ranking |
| `startingSix` | Proposed official initial physical location |

Namespaces isolate identities from future providers. A jersey number or lineup code is never a player identity. Duplicate persistent players, duplicate match-player IDs or conflicting roster slots stop confirmation before a partial game can be stored. Recoverable position/assignment inconsistencies retain the record and show warnings. Original provider assignment metadata remains distinct from operator changes.

For example, provider game `966` has local game ID `hz-966`; match-player ID `48611` maps to local roster-player ID `hz-966-roster-48611`, while persistent player ID `90` maps to `person_id: "hz-player-90"`. The roster record retains its external player/match-player IDs separately. Its `source` assignment metadata includes `roster_code`, `raw_position`, `parsed_role`, `line_number`, `starting_six` and `roster_key`. These source values remain available when a local position, number, lineup or preset changes.

### Roster-code rules

Codes remain three-character strings. `101` is the designated starting-goalkeeper slot and `102` the backup; neither is parsed as a skater line. Skater codes use category, line number and within-line position:

| Category | Line digit | Last digit | Assigned role |
| --- | --- | --- | --- |
| `2` defence | `1`–`9` | `1`, `2` | Left defence, right defence |
| `3` forward | `1`–`9` | `1`, `2`, `3` | Left wing, centre, right wing |

Examples: `211`/`212` form defence pair 1; `221`/`222` pair 2; `311`/`312`/`313` forward line 1; `343` is fourth-line right wing. Goalkeepers map to domain position `G`, defenders to `D` with left/right role metadata, and forwards to `LW`, `C` or `RW`. Raw broad values `DE` and `DF` both indicate defence; a valid structured code takes precedence if the broad value conflicts. The mismatch remains visible as a warning.

Unknown codes retain their raw value and require review rather than silently inventing a specific slot. Groups are generated from valid assignments regardless of JSON order, with missing slots left empty. Incomplete lines/pairs remain reviewable; the importer does not require a fixed roster size. Editing a local preset does not rewrite an official source code. Restoring imported lines is an explicit operation.

### Preview, confirmation and mid-game coverage

The operator loads a source preview before persistence, reviews both complete rosters and line/pair groups, and chooses official starting locations or a manually established current lineup. `startingSix` governs the official lineup even when it disagrees with goalkeeper slot `101` or first-line assignments; disagreements and unusual skater/goalkeeper counts produce warnings. A match completed in the provider can still be imported for training or reconstruction.

Confirmation includes the preview fingerprint. If the preview no longer matches, reload and review rather than accepting unseen changes. Initial tracking starts with zero accumulated local TOI. A partial-match session records its coverage period/countdown and creates no shifts before that coverage point. New imports begin paused, so source status cannot accidentally start local timing.

The local imported game ID is deterministic (`hz-966` for provider game `966`). Importing it again returns the existing local game, including all history and corrections, rather than replacing its starting six. Source fetching and parsing happen before the transactional write; failed fetch, schema validation, identity reconciliation or database commit cannot leave a half-created game.

### Safe roster refresh

Refresh is a two-step operation: force-fetch source data and review a diff, then confirm with its fingerprint and the latest local game version. No source update directly moves a tracked player or starts/stops the local clock.

- Add genuinely new roster entries on the bench with zero prior TOI.
- Keep players removed from the source roster and flag their source absence; preserve IDs, locations and historical references.
- Compare changed metadata to the prior source baseline. Apply source changes where the local field still follows that baseline; preserve local edits and report their conflict for review. Existing local eligibility choices remain authoritative even if the latest source considers a player available.
- Keep operator lines and pairs by default. `restore_lines: true` explicitly restores the latest imported assignments.
- Preserve shifts, movement/penalty events, clock state, accumulated TOI and recommendation observations.

A stale preview or local version conflict requires a fresh review. Timeouts and failed force-refreshes do not apply a cached response as if it were new; the already imported local state remains available. Source snapshots and baselines make subsequent comparisons auditable without coupling domain movements to raw provider fields.

### Failure handling and tests

The API returns structured import errors for invalid IDs/configuration, missing games, rate limits, upstream failures, timeouts, invalid JSON/schema and identity conflicts. Warnings cover recoverable record/line inconsistencies. Operator messages avoid raw stack traces. Do not poll this mostly pre-match endpoint aggressively; use the preview cache and an intentional refresh.

Ordinary automated tests use a checked-in source fixture and mocked transport/provider calls. They do not require provider access. Integration tests exercise import/reimport, partial coverage, player additions/removals, metadata/line changes, local-edit preservation, rollback and tracking across refresh. A live provider check, when performed, is separately recorded in validation notes.

## Authentication and permissions

`resolve_principal` is the authentication seam. It returns `Principal(actor, team_ids, can_control_clock)`. `team_ids=None` means unrestricted team access; a set restricts movement/undo/corrections to those teams. Match setup, game creation and clock control require a supervisor with unrestricted team access and clock permission. The server writes the authenticated actor into audit records and ignores a client-supplied actor.

For a protected standalone prototype, configure a bearer token or `TOI_OPERATOR_TOKENS` JSON. Replace this resolver with trusted website session/token verification for production. Verify token issuer, audience, expiry and signature through the website's existing mechanism. Never trust a browser-provided actor or team list. Keep website credentials out of Vite build-time variables and static assets.

Browser WebSockets cannot add an arbitrary Authorization header. The prototype accepts its operator token through a query parameter. For production, prefer a secure same-site session cookie or short-lived, single-use WebSocket ticket, and redact authentication query parameters from access logs. Terminate TLS at the existing ingress. Team assignment controls writes; current prototype readers can see the complete game once authenticated, so add separate read scoping if required.

## Scoreboard / OCR clock observations

`ClockProvider` and `ClockObservation` describe the adapter contract. A bridge can read the existing scoreboard/OCR pipeline, normalize its values and submit an observation to `POST /api/v1/games/{game_id}/clock/observations`:

```json
{
  "request_id": "unique-clock-observation-id",
  "expected_version": 12,
  "period": 1,
  "remaining_ms": 1034000,
  "running": true,
  "observed_at_ms": 1792130400000,
  "source": "scoreboard"
}
```

`observed_at_ms` is the observation's UTC Unix timestamp in milliseconds; replace the example value with the actual source timestamp. Fetch the current version first and resynchronize after HTTP 409. The request ID makes transport retries safe. Use a supervisor principal, or replace authorization with a narrowly scoped service principal before deployment.

Manual clock ownership is the default. Set `TOI_CLOCK_SOURCE=scoreboard` to accept only that exact external source name; competing sources and out-of-order readings are rejected. Observations older than `TOI_CLOCK_STALE_MS` (10 seconds by default) or more than two seconds in the future are rejected. The external-feed watchdog pauses a running clock when updates go stale; it never invents a missing feed interval. A manual supervisor clock action clears the old external observation and takes over locally, so a stale feed cannot prevent manual fallback. A subsequent fresh observation from the configured provider resumes external ownership. Coordinate the bridge during a deliberate handover to prevent an unexpected automatic takeover by its next reading.

Clock corrections reanchor the displayed countdown and preserve accumulated playing time. They do not retroactively assign a dropped scoreboard interval to players. If retrospective time correction is later required, introduce an explicit timeline correction event with tests for every affected shift. OCR confidence filtering, debounce and feed health should live in the bridge; this application does not call an undocumented OCR endpoint.

## Consume statistics and live events

Fetch `GET /api/v1/games/{id}/state` on connection and after any conflict. Subscribe to `/api/v1/games/{id}/ws` for versioned live messages. Initial and mutation messages include a full `state`. Idle `clock.tick` messages instead contain `clock`, a `toi` array keyed by `player_id`, `warnings` and `strengths`; they omit roster, event and shift histories. Merge a tick only when its version matches the loaded snapshot. Discard older versions; if a tick has a newer version, fetch `/state` before merging it because a movement may have been missed. Reconnect with exponential backoff and fetch the current snapshot.

For live display between authoritative clock updates, interpolate only while `clock.running`, cap the increment at `remaining_ms`, and add that increment to the total/current shift of players currently on the ice. A snapshot or equal-version tick reanchors the interpolation. Never store these client display estimates as official shifts. No high-frequency event stream is required for correct TOI.

The roster/state provides each player's physical location; the statistics endpoint gives TOI and shifts. Consumers can filter `location == "ON_ICE"`, order players by `total_toi_ms`, find the current two defenders by position, or list `PENALTY_BOX` players. Recommendation responses include observed combinations and explainable rankings. A present-day pair may contain more or fewer than two defenders, so a graphic should tolerate unusual combinations.

For a commentary dashboard, create a read-only component using snapshots and format integer milliseconds as `M:SS`. For a vMix adapter, use the same state to create the required title-data, XML, JSON or API calls **in a separate consumer** once the graphics contract is known. Keep vMix names, field limits and update cadence outside the domain and public statistics model.

Special-teams TOI is `null` until an authoritative playing-strength timeline exists. A penalty-box occupant alone does not identify a power play: misconducts and coincidental penalties are separate rules. Formal penalty processing should feed strength segments to an explicitly tested future projection.

## Separate backend deployment and migration

Build `Dockerfile.backend`, set `TOI_DATABASE_URL`, CORS, clock ownership and authentication, then expose it through the existing ingress. The startup script applies Alembic migrations and starts one worker. PostgreSQL is the deployment database; SQLite is intended for local development and tests. Back up the database before upgrades and retain external-ID mappings and the full event/audit history when migrating data.

The WebSocket hub currently runs in process. Multiple browsers and concurrent operators are supported, but multiple API worker processes or replicas need shared pub/sub before live updates can be reliable. Add Redis or PostgreSQL notifications at that boundary, publish only committed changes, and retain version checks and idempotency receipts in PostgreSQL. Health monitoring should distinguish API/database availability from an upstream scoreboard feed becoming stale.
