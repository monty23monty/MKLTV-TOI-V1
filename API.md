# API v1

This file describes the implemented v1 endpoints. Its WebSocket sends updates and commands enter over HTTP. The additive v2 API supports duplex commands, authoritative clock input, per-player TOI, on-ice lineups and replayable recorded changes; see [SERVICE_INTERFACE.md](SERVICE_INTERFACE.md) and [UPDATING.md](UPDATING.md).

The backend exposes [OpenAPI JSON](http://localhost:8000/openapi.json) and [interactive API documentation](http://localhost:8000/docs). All JSON timing fields ending `_ms` are integer milliseconds. Player IDs are opaque strings; jersey numbers are roster attributes.

When token authentication is configured, send `Authorization: Bearer <token>` on HTTP requests. In local demo mode, no credential is required. The authenticated principal supplies the audit actor; a client `actor` field is ignored for identity. Health is public.

## HTTP resources

All game paths below begin `/api/v1/games/{game_id}`.

| Method | Path | Response / purpose |
| --- | --- | --- |
| GET | `/health` | API/database health (outside game prefix) |
| GET | `/ready` | Database readiness and diagnostic dispatcher status (outside game prefix) |
| GET | `/api/v1/games` | Array of game metadata and current versions |
| POST | `/api/v1/games` | Create a new fictional match; body `{name?, home_name?, away_name?}`; returns snapshot, HTTP 201 |
| GET | `/api/v1/integrations/hokejovy-zapis/games/{external_game_id}/preview` | Normalize source match/rosters; optional `?force=true` bypasses preview cache |
| POST | `/api/v1/games/import` | Confirm reviewed provider preview and create/reopen an imported session |
| GET | `/state` | Authoritative full snapshot |
| GET | `/teams` | Team records including side, colors and external IDs |
| GET | `/roster` | Roster records and stored physical locations; use `/state` or `/statistics` for live timing |
| GET | `/lines` | Current operator line/pair configurations, including imported groups |
| POST | `/roster/refresh` | Review or explicitly apply a force-fetched source metadata diff |
| GET | `/locations` | `{version, players:[{player_id,team_id,location,eligible}]}` |
| GET | `/statistics` | `{game_id,version,clock,players,strength_classification}`; players sorted by descending TOI |
| GET | `/shifts` | Shift array; optional `?player_id=...` |
| GET | `/events` | Authoritative movement, clock, setup and correction history, including inactive events |
| GET | `/audit` | Command audit with actor, request ID, version, timestamp, previous/new locations and clock |
| GET | `/clock` | Current authoritative clock |
| GET | `/suggestions?team_id=...&outgoing_ids=id1,id2` | Eligible bench ranking, groups and observed combinations |
| POST | `/actions` | Transactional command; returns authoritative snapshot |
| POST | `/clock/observations` | External clock input; supervisor and configured source required |
| GET | `/clock/authority` | Supervisor-only v2 ownership, state version and configured source/service choices |
| POST | `/clock/authority` | Supervisor-only explicit manual/external clock handover; returns v1 snapshot |
| WS | `/ws` | Snapshot/event stream; optional `?token=...` |

`POST /api/v1/games` starts from the demonstration provider. For an external roster, use the preview/import workflow below. Imported locations and metadata become local game state; ordinary tracking does not depend on source availability.

The authority routes require `TOI_STREAM_ENABLED=true`. `GET /clock/authority` returns `{version,authority,sources}`, with `sources:[{source,service_id}]` restricted to configured, unexpired clock writers for that game. `POST /clock/authority` accepts `{request_id,expected_version,mode:"external",source,service_id}` or `{request_id,expected_version,mode:"manual"}`. External ownership starts stale/paused until a valid fenced observation arrives. Manual start/pause/set/advance also takes ownership back. Reuse the exact request ID and body after a lost response; version conflicts require resynchronization.

Legacy `/clock/observations` is available only before a game activates v2. Activated games reject unfenced input; use the scoped v2 connection and ownership generation documented in [SERVICE_INTERFACE.md](SERVICE_INTERFACE.md#7-authoritative-clock-connection).

## Hokejový Zápis import and refresh

Preview a positive numeric source Game ID with `GET /api/v1/integrations/hokejovy-zapis/games/966/preview`. The backend fetches and normalizes the configured provider endpoint. `force=true` bypasses its expiring preview cache. The preview exposes normalized game/team/player/line information, source warnings and a fingerprint; it does not create a tracking session or expose the complete raw provider response.

Preview fields are `provider`, `external_game_id`, `schema_version`, `fetched_at_ms`, `preview_fingerprint`, `game`, `teams`, `players`, `lines`, `warnings` and `cache`. Cache metadata identifies hits, age, expiry and stale recovery; a stale result also carries `cache_error`. Teams retain source `external_id`, local `id`, name, shortcut, side and provider. Game metadata includes external status, stadium, coaches, referees and results.

A normalized player separates match identity and persistent identity:

```json
{
  "id": "hz-966-roster-48611",
  "person_id": "hz-player-90",
  "external_player_id": "90",
  "match_player_id": "48611",
  "team_id": "hz-team-5",
  "name": "Jordan Hedley",
  "number": 1,
  "position": "G",
  "assigned_role": "GK",
  "eligible": true,
  "location": "ON_ICE",
  "source": {
    "roster_key": "101",
    "roster_code": "101",
    "raw_position": "GK",
    "parsed_role": "GK",
    "line_number": null,
    "starting_six": true,
    "review_required": false
  }
}
```

This example omits additional display/provenance fields. A source line includes `id`, `team_id`, `name`, `kind`, `player_ids`, `line_number` and `slots`. Slots use `LW`/`C`/`RW` or `LD`/`RD`; missing memberships are `null`, while `player_ids` contains only actual members. The preview is review data, not an additional mutable tracking state.

Confirm the reviewed result:

```json
{
  "provider": "hokejovy-zapis",
  "external_game_id": "966",
  "preview_fingerprint": "fingerprint-returned-by-preview",
  "starting_mode": "official"
}
```

Send this body to `POST /api/v1/games/import`. Optional `players` patches and `lines` let the operator confirm adjusted setup. `starting_mode: "manual"` supports establishing the current lineup; optional `coverage: {"period": 2, "remaining_ms": 900000}` records a partial-match start without fabricating earlier TOI. The clock begins paused. Source completion status remains source metadata and does not block local tracking.

The deterministic local game ID is `hz-966`. Import returns HTTP 200 with a snapshot. Repeated import returns its existing state instead of duplicating or resetting it, and does not require the provider to be available. A mismatched preview fingerprint requires a fresh preview/review. Preview, import and source-refresh endpoints require match-supervisor permission; writes are transactional. An explicitly labeled stale cached preview can be reviewed and confirmed for import, while forced refresh failures remain errors.

To refresh an imported game's source roster, post this to `/api/v1/games/hz-966/roster/refresh`:

```json
{
  "request_id": "unique-roster-refresh-request",
  "expected_version": 0,
  "confirm": false,
  "restore_lines": false
}
```

The preview response contains `changes`, `warnings`, `preview_fingerprint` and `version`. After review, send `confirm: true` with that fingerprint and the current expected version. Use the same request ID/body only to retry an identical confirmed request; a new intentional action needs a new request ID.

Refresh appends genuinely new players on the bench, flags missing source players without deleting their tracked identity, and reconciles metadata against the previous source baseline. Existing physical locations, shifts, game clock and event history remain authoritative. Existing local eligibility choices, field edits and line configurations are preserved; `restore_lines: true` explicitly restores current source lines/pairs. Source refresh never reapplies `startingSix` to active player locations. Successful confirmation returns a full snapshot and emits `state.resync`; a duplicate confirmed request returns its receipt before contacting the provider.

Provider errors are structured failures; a failed fetch/validation/commit does not leave a partially imported match or partly refreshed roster. See [INTEGRATION.md](INTEGRATION.md) for source configuration, roster codes, identity namespaces, caching and reconciliation rules.

## Snapshot shape

```json
{
  "game": {
    "id": "demo",
    "name": "Milton Keynes Storm vs Northbank Wolves",
    "status": "LIVE",
    "competition": "Demonstration · fictional rosters",
    "external_id": null,
    "period_lengths_ms": [1200000, 1200000, 1200000]
  },
  "teams": [],
  "players": [],
  "lines": [],
  "clock": {
    "period": 1,
    "remaining_ms": 1200000,
    "running": false,
    "playing_ms": 0,
    "anchor_unix_ms": 1792130400000,
    "period_playing_ms": {"1": 0},
    "period_spans": []
  },
  "events": [],
  "shifts": [],
  "version": 0,
  "warnings": [],
  "strengths": {"home": 5, "away": 5},
  "server_time_ms": 1792130400000
}
```

The arrays above are abbreviated. A player contains `id`, `team_id`, `external_id`, `name`, `number`, `position` (`C`, `LW`, `RW`, `D`, `G`), `eligible`, `location`, `total_toi_ms`, `current_shift_ms`, `completed_shifts`, `average_shift_ms`, `longest_shift_ms`, `toi_by_period` and `strength_toi`. Location is exactly `ON_ICE`, `BENCH` or `PENALTY_BOX`. `strength_toi` is `null`: formal strength classifications are unavailable. Current physical skater counts in `strengths` are not formal penalty classifications.

A shift contains `id`, `player_id`, `team_id`, `start_playing_ms`, nullable `end_playing_ms`, `start_period`, nullable `end_period`, `period`, `source_event_id` and `source`. Public `start_ms`/`end_ms` aliases refer to the same cumulative playing-time coordinate. An open shift is excluded from completed-shift count and average, but included in total TOI and longest-shift duration. `toi_by_period` uses period spans to handle a shift crossing periods.

Events include `id`, `request_id`, `type`, `payload`, `at_playing_ms`, `playing_ms` (alias), `period`, `active`, `voided` (inverse alias), `actor` and `created_at_ms`. Undo/correction adds links to the affected event; inactive history is retained.

## Commands

Send every command to `POST /api/v1/games/{id}/actions` in this envelope:

```json
{
  "request_id": "a-new-unique-command-id",
  "expected_version": 0,
  "type": "substitute",
  "payload": {
    "team_id": "demo-home",
    "outgoing_ids": ["demo-home-p02"],
    "incoming_ids": ["demo-home-p05"],
    "at_playing_ms": 0
  }
}
```

Use a new UUID for each intentional command. Retry a lost response with **exactly the same request ID and body**. A stored receipt returns the original response without applying the command again. Reusing the ID with a different body or principal returns HTTP 409. Check response versions before replacing newer client state with a receipt snapshot.

`expected_version` must equal the current state version. On HTTP 409, retrieve `/state` and let the operator select the action again; do not silently overwrite a newer lineup. Requests with invalid contracts or illegal movements return 422; unavailable resources return 404; invalid credentials return 401; unauthorized team/supervisor actions return 403. Errors have `detail`, and conflicts include `resync_required: true`.

| Type | Payload | Behavior |
| --- | --- | --- |
| `substitute` | `{team_id,outgoing_ids:[],incoming_ids:[],at_playing_ms?}` | Atomic individual or arbitrary group change; outgoing players must be on ice; incoming players eligible on bench |
| `move` | `{player_id,to}` | One independent location transition, including all penalty-box/bench/ice directions |
| `clock` | `{operation:"start"}` | Start/resume a positive countdown |
| `clock` | `{operation:"pause"}` | Stop playing-time accumulation |
| `clock` | `{operation:"set",period?,remaining_ms?,period_lengths_ms?}` | Correct countdown/configuration and leave the clock paused |
| `clock` | `{operation:"advance",period_lengths_ms?}` | Advance one configured period, reset to its duration and pause |
| `undo` | `{event_id?}` | Atomically undo the latest movement, or selected movement if replay remains valid |
| `correct` | `{event_id,replacement:{type:"substitute"\|"move",payload:{...}}}` | Replace the original movement at its original time, preserving the audit |
| `setup` | `{players?:[],lines?:[],game?:{...}}` | Edit roster/starting lineup, configured lines and game metadata |

Capture `at_playing_ms` when selecting the outgoing player for a replacement, and send that same timestamp for the complete group. It must fall within the current accumulated timeline and cannot precede a later confirmed movement. Omit it to use the backend's current playing time. Movements do not require equal group sizes or six players per team; warnings identify unusual lineups.

An unavailable player must be benched before being excluded; they cannot enter the ice or appear as an eligible replacement. Undo/correction rebuilds the original lineup and active shifts from valid history. If changing a historical event would invalidate a later dependent movement, the request returns a conflict; correct or undo the dependent movement first.

### Match setup

Each `players` item identifies an existing player by `id`, with optional `name`, integer `number` (0–99), `position`, boolean `eligible`, `external_id` and `location`. Before tracking, a new ID adds a player if it supplies all of `id`, `team_id`, `name`, `number`, `position`, `eligible` and `location`. Starting locations and new roster players can be configured only before playing time or movement history exists. Thereafter use movements and corrections for location changes. Eligible jersey numbers must be distinct within each team.

`lines` replaces the full line configuration with records `{id,team_id,name,kind,player_ids}` and optional `slots`/`line_number` assignment metadata. Kinds are `FORWARD`, `DEFENCE` and `SPECIAL`; membership must belong to the named team. The domain does not impose line sizes, so mixed special-team groups and empty planned lines are possible. Empty groups are not offered as substitution suggestions.

`game` supports `name`, `competition`, `scheduled_start`, `external_id`, `status` and `period_lengths_ms`; team names can be supplied as `home_name` and `away_name`. Setup and clock operations require a match supervisor. Do not modify the database projections directly.

### External clock input

Submit `{request_id,expected_version,period,remaining_ms,running,observed_at_ms,source}` to `/clock/observations`. The path identifies the game. `observed_at_ms` is a UTC Unix timestamp. `TOI_CLOCK_SOURCE` must equal `source` and must not be `manual`. Stale, excessively future and out-of-order observations are rejected; see [INTEGRATION.md](INTEGRATION.md) for ownership, watchdog and dropped-feed semantics.

## Suggestions

`GET /suggestions?team_id=demo-home&outgoing_ids=demo-home-p02` returns:

```json
{
  "players": [{
    "player_id": "demo-home-p05",
    "score": 28.0,
    "reasons": ["Same position"],
    "eligible": true,
    "observations": 0,
    "rest_ms": 0,
    "confidence": "cold_start"
  }],
  "groups": [],
  "combinations": []
}
```

The example is abbreviated; all eligible bench players are returned. `score` is a weighted ranking, not a probability. Confidence is `cold_start`, `developing` or `established`, based on supporting direct replacement observations. Group suggestions contain `player_ids`, `name`, `score`, `reasons`, `eligible` and `observations`. Observed combinations report `player_ids`, `name`, `observations` and current bench eligibility. An on-ice or penalty-box player is never offered as an ordinary bench replacement.

Weights in `recommendations.DEFAULT_WEIGHTS` are independently overrideable in the pure `recommend` function. Observations derive from active authoritative events, configured groups and reconstructed shifts; undo and historical corrections affect the next ranking immediately.

## WebSocket stream

This is a server-to-client application stream. Sending a command frame to it does not execute an action; use the HTTP command/clock endpoints above.

Connect to `ws://localhost:8000/api/v1/games/demo/ws` (`wss:` under TLS). When configured, supply `?token=<URL-encoded bearer token>` or an Authorization header if the client supports it. Browser origins must match the same origin or an entry in `TOI_CORS_ORIGINS`.

```json
{
  "type": "substitution.updated",
  "game_id": "demo",
  "version": 3,
  "request_id": "command-id",
  "server_time_ms": 1792130400000,
  "state": {"version": 3, "...": "full snapshot"}
}
```

Event types are `state.resync`, `player.location.updated`, `substitution.updated`, `substitution.group.updated`, `penalty.location.updated`, `clock.updated`, `clock.tick` and `correction.updated`. Connection and persisted mutation messages include a full `state`. State resync is sent at connection/setup and when a slow subscriber's queue is replaced with a fresh snapshot.

While the socket is idle, a compact tick is sent about once per second:

```json
{
  "type": "clock.tick",
  "game_id": "demo",
  "version": 3,
  "server_time_ms": 1792130400000,
  "clock": {"period": 1, "remaining_ms": 1160000, "running": true, "playing_ms": 40000},
  "toi": [{
    "player_id": "demo-home-p01",
    "total_toi_ms": 40000,
    "current_shift_ms": 40000,
    "completed_shifts": 0,
    "average_shift_ms": 0,
    "longest_shift_ms": 40000,
    "toi_by_period": {"1": 40000, "2": 0, "3": 0}
  }],
  "warnings": [],
  "strengths": {"home": 5, "away": 5}
}
```

The clock and TOI array above are abbreviated. A tick has no `state`, roster, events or shifts. The command version increments for persisted commands; ticks can share a version while timing advances. Merge equal-version ticks into the current state by player ID and reanchor clock interpolation. Discard lower versions. A higher-version tick indicates a missed mutation: fetch `/state` instead of merging potentially stale player locations. `request_id` and top-level `server_time_ms` are optional across full-snapshot event types. Reconnect and fetch `/state`; do not assume every transient event will reach a disconnected or slow client.
