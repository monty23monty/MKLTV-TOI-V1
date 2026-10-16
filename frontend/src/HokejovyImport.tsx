import { createUuid } from './uuid'
import { useState } from 'react'
import { ArrowLeft, ArrowRight, Check, CheckCircle2, ChevronDown, Clock3, Database, Download, FileCheck2, Globe2, Layers3, LoaderCircle, Plus, RefreshCw, Shield, ShieldAlert, Users, X } from 'lucide-react'
import type { Line, Player, Position, Snapshot, Team } from './types'
import { api } from './useGame'

type Role = 'GK' | 'G' | 'D' | 'LD' | 'RD' | 'LW' | 'C' | 'RW'
type ImportedPlayer = Pick<Player, 'id' | 'team_id' | 'name' | 'number' | 'position' | 'eligible' | 'location'> & {
  person_id?: string
  assigned_role?: string
  display_position?: string
  source: { roster_code?: string; raw_position?: string; parsed_role?: string; line_number?: number | null; starting_six?: boolean | null; [key: string]: unknown }
}
type ImportedLine = Line & { slots?: Record<string, string | null>; line_number?: number }
type Preview = { provider: string; external_game_id: string; schema_version: number; fetched_at_ms: number; preview_fingerprint: string; warnings: string[]; game: { name: string; external_status?: string; stadium?: string; [key: string]: unknown }; teams: Team[]; players: ImportedPlayer[]; lines: ImportedLine[] }
type RefreshDiff = { changes: { type: string; player_id?: string; field?: string; before?: unknown; after?: unknown; message?: string }[]; warnings: string[]; preview_fingerprint: string; version: number }
const roles: Role[] = ['GK', 'G', 'D', 'LD', 'RD', 'LW', 'C', 'RW']
const rolePosition = (role: string): Position => role === 'GK' || role === 'G' ? 'G' : role === 'LD' || role === 'RD' ? 'D' : role as Position
const playerRole = (player: ImportedPlayer): string => {
  const role = player.assigned_role || player.source.parsed_role
  if (role && roles.includes(role as Role)) return role
  if (player.source.position_unresolved) return ''
  return player.position === 'G' ? 'GK' : player.position
}
const sourceStatus = (status: unknown) => status === 'end' || status === 'completed' ? 'Completed' : String(status || 'Not provided')
const slotNames = (kind: Line['kind']) => kind === 'DEFENCE' ? ['LD', 'RD'] : ['LW', 'C', 'RW']
const slotsFor = (line: ImportedLine) => line.slots || Object.fromEntries(slotNames(line.kind).map((slot, index) => [slot, line.player_ids[index] || null]))
const showValue = (value: unknown) => value === undefined || value === null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value)

export function HokejovyImport({ onImported, onCancel }: { onImported: (state: Snapshot) => Promise<void>; onCancel: () => void }) {
  const [gameId, setGameId] = useState('')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [players, setPlayers] = useState<ImportedPlayer[]>([])
  const [lines, setLines] = useState<ImportedLine[]>([])
  const [busy, setBusy] = useState<'fetch' | 'import' | null>(null)
  const [error, setError] = useState('')
  const [startingMode, setStartingMode] = useState<'official' | 'manual'>('official')
  const [period, setPeriod] = useState(1)
  const [remaining, setRemaining] = useState('20:00')
  const fetchMatch = async (force = false) => {
    if (!/^[1-9]\d{0,14}$/.test(gameId.trim())) { setError('Enter a positive numeric Game ID with at most 15 digits, for example 966.'); return }
    setBusy('fetch'); setError('')
    try {
      const result = await api<Preview>(`/api/v1/integrations/hokejovy-zapis/games/${gameId.trim()}/preview?force=${force}`, undefined, 45000)
      setPreview(result); setPlayers(structuredClone(result.players)); setLines(structuredClone(result.lines)); setStartingMode('official'); setPeriod(1); setRemaining('20:00')
    } catch (e) { setError((e as Error).name === 'AbortError' ? 'The source did not respond in time. Try fetching the match again.' : (e as Error).message) }
    finally { setBusy(null) }
  }
  const changePlayer = (id: string, changes: Partial<ImportedPlayer>) => setPlayers(previous => previous.map(player => player.id === id ? { ...player, ...changes } : player))
  const setSlot = (lineId: string, slot: string, playerId: string) => {
    const line = lines.find(entry => entry.id === lineId)!
    setLines(previous => previous.map(entry => {
      if (entry.id !== lineId && (entry.team_id !== line.team_id || entry.kind !== line.kind || !playerId)) return entry
      const slots = { ...slotsFor(entry) }
      for (const key of Object.keys(slots)) if (slots[key] === playerId) slots[key] = null
      if (entry.id === lineId) slots[slot] = playerId || null
      return { ...entry, slots, player_ids: Object.values(slots).filter((id): id is string => !!id) }
    }))
  }
  const confirm = async () => {
    if (!preview) return
    setError('')
    const match = remaining.match(/^(\d{1,2}):([0-5]\d)$/)
    if (!match) { setError('Coverage time must use minutes:seconds, for example 12:35.'); return }
    const remainingMs = (Number(match[1]) * 60 + Number(match[2])) * 1000
    if (remainingMs > 1200000) { setError('Remaining coverage time cannot exceed 20:00 for a regulation period.'); return }
    if (players.some(player => !player.name.trim() || !String(player.number).trim() || !Number.isInteger(Number(player.number)) || Number(player.number) < 0 || Number(player.number) > 99)) { setError('Every player needs a name and a whole jersey number between 0 and 99.'); return }
    if (players.some(player => player.eligible && !playerRole(player))) { setError('Choose an assigned role for every eligible player with an unresolved source position.'); return }
    for (const team of preview.teams) {
      const dressed = players.filter(player => player.team_id === team.id && player.eligible)
      if (new Set(dressed.map(player => Number(player.number))).size !== dressed.length) { setError(`${team.name} has duplicate dressed jersey numbers. Correct them or mark the affected player unavailable.`); return }
    }
    if (lines.some(line => !line.name.trim())) { setError('Give each line or pair a name.'); return }
    setBusy('import')
    try {
      const snapshot = await api<Snapshot>('/api/v1/games/import', { method: 'POST', body: JSON.stringify({ provider: 'hokejovy-zapis', external_game_id: preview.external_game_id, preview_fingerprint: preview.preview_fingerprint, players: players.map(player => ({ id: player.id, name: player.name.trim(), number: Number(player.number), position: player.position, ...(playerRole(player) ? { assigned_role: playerRole(player) === 'G' ? 'GK' : playerRole(player) } : {}), eligible: player.eligible, location: player.eligible ? player.location : 'BENCH' })), lines, starting_mode: startingMode, coverage: { period, remaining_ms: remainingMs } }) }, 45000)
      await onImported(snapshot)
    } catch (e) { setError((e as Error).name === 'AbortError' ? 'The import response timed out. Confirm again to safely resume the same match; a repeated Game ID does not create duplicates.' : (e as Error).message) }
    finally { setBusy(null) }
  }
  const restore = () => { if (preview) { setPlayers(structuredClone(preview.players)); setLines(structuredClone(preview.lines)); setStartingMode('official') } }
  return <section className="source-import"><div className="source-intro"><div><span className="eyebrow"><Globe2 size={13} />CONNECTED ROSTER PROVIDER</span><h2>Import from Hokejový Zápis</h2><p>Load the official match, starting players, forward lines and defensive pairs. Review everything before creating your tracking session.</p></div><button className="subtle-button" onClick={onCancel} disabled={busy === 'import'}><ArrowLeft size={15} />Back to tracking</button></div>
    <form className="source-fetch" onSubmit={event => { event.preventDefault(); void fetchMatch() }}><label>Numeric Game ID<input inputMode="numeric" pattern="[1-9][0-9]{0,14}" maxLength={15} required value={gameId} onChange={event => setGameId(event.target.value.replace(/[^0-9]/g, ''))} placeholder="e.g. 966" disabled={!!busy} /></label><button className="primary-button" disabled={!!busy}>{busy === 'fetch' ? <LoaderCircle size={17} className="spin" /> : <Download size={17} />}{busy === 'fetch' ? 'Fetching match…' : 'Fetch match'}</button><div><Shield size={17} /><span>Preview only. Fetching does not create a session.<small>Source requests are made securely by the backend.</small></span></div></form>
    {error && <div className="form-error" role="alert">{error}</div>}
    {preview && <><div className="source-match-preview"><div className="source-match-teams">{preview.teams.map(team => <div key={team.id}><span className="eyebrow">{team.side.toUpperCase()} · {team.short_name}</span><h3>{team.name}</h3><p><Users size={13} />{players.filter(player => player.team_id === team.id).length} players<span>·</span>{lines.filter(line => line.team_id === team.id && line.kind === 'FORWARD').length} forward lines<span>·</span>{lines.filter(line => line.team_id === team.id && line.kind === 'DEFENCE').length} defensive pairs</p></div>)}</div><div className="source-metadata"><span><small>STADIUM</small>{preview.game.stadium || 'Not provided by source'}</span><span><small>EXTERNAL MATCH STATUS</small>{sourceStatus(preview.game.external_status)}</span><span><small>LOCAL TOI SESSION</small>Not started / resume if imported</span><span><small>SOURCE REFERENCE</small>Hokejový Zápis · #{preview.external_game_id}</span></div><p className="source-status-note"><Database size={13} />Completed source matches can be used for training or reconstruction. Reimporting this Game ID resumes its existing session and preserves all recorded shifts.</p></div>
      {!!preview.warnings.length && <div className="source-warnings"><div><ShieldAlert size={17} /><strong>Source checks · {preview.warnings.length} {preview.warnings.length === 1 ? 'warning' : 'warnings'}</strong></div><ul>{preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div>}
      <div className="coverage-editor"><div><span className="eyebrow">STARTING STATE</span><h3>Establish your tracking coverage</h3><p>All TOI starts at zero from this clock position. No earlier playing time is reconstructed.</p></div><div className="starting-mode"><label><input type="radio" name="starting-mode" checked={startingMode === 'official'} onChange={() => { setStartingMode('official'); setPlayers(previous => previous.map(player => ({ ...player, location: player.eligible && player.source.starting_six ? 'ON_ICE' : 'BENCH' }))) }} /><span><strong>Official starting lineup</strong><small>Use each player’s startingSix flag</small></span></label><label><input type="radio" name="starting-mode" checked={startingMode === 'manual'} onChange={() => setStartingMode('manual')} /><span><strong>Manual current lineup</strong><small>Review “On ice” below for a mid-game start</small></span></label></div><div className="coverage-time"><label>Coverage period<select value={period} onChange={event => setPeriod(Number(event.target.value))}>{[1, 2, 3].map(value => <option key={value} value={value}>Period {value}</option>)}</select></label><label>Remaining time<input inputMode="numeric" value={remaining} onChange={event => setRemaining(event.target.value)} placeholder="20:00" /></label></div>{(period > 1 || remaining !== '20:00') && <div className="coverage-warning"><Clock3 size={15} />Partial-match coverage · starts at P{period} {remaining}. Check the current lineup before confirming.</div>}</div>
      <div className="source-review-heading"><div><span className="eyebrow">ROSTER & LINEUP REVIEW</span><h3>Official assignments. Operator control.</h3></div><div><button className="subtle-button" disabled={!!busy} onClick={restore}><RefreshCw size={14} />Restore source draft</button><button className="subtle-button" disabled={!!busy} onClick={() => void fetchMatch(true)}><Globe2 size={14} />Reload source preview</button></div></div>
      <div className="import-team-grid">{preview.teams.map(team => {
        const roster = players.filter(player => player.team_id === team.id)
        const teamLines = lines.filter(line => line.team_id === team.id)
        const groupNames = new Map<string, string>()
        roster.forEach(player => {
          const role = player.source.parsed_role
          const category = role === 'GK' || player.position === 'G' ? 'goalies' : role === 'LD' || role === 'RD' || player.position === 'D' ? 'defence' : 'forwards'
          const key = category === 'goalies' ? '0' : `${category === 'defence' ? '1' : '2'}-${player.source.line_number || 0}`
          groupNames.set(key, category === 'goalies' ? 'Goalkeepers' : `${category === 'defence' ? 'Defensive pair' : 'Forward line'} ${player.source.line_number || 'unassigned'}`)
        })
        const playerGroup = (player: ImportedPlayer) => {
          const role = player.source.parsed_role
          if (role === 'GK' || player.position === 'G') return '0'
          return `${role === 'LD' || role === 'RD' || player.position === 'D' ? '1' : '2'}-${player.source.line_number || 0}`
        }
        const onIce = roster.filter(player => player.eligible && player.location === 'ON_ICE')
        const skaters = onIce.filter(player => player.position !== 'G').length
        const goalies = onIce.filter(player => player.position === 'G').length
        return <section className="import-team" key={team.id}><div className="import-team-heading"><div><span className="eyebrow">{team.side.toUpperCase()} · {team.short_name}</span><h3>{team.name}</h3></div><span>{onIce.length} on ice · {roster.filter(player => player.eligible).length} dressed</span></div>{(skaters !== 5 || goalies !== 1) && <p className="lineup-warning"><ShieldAlert size={14} />Starting lineup: {skaters} skaters, {goalies} goalkeepers. Review this unusual count; import remains available.</p>}
          <div className="import-roster-table-wrap"><table className="import-roster-table"><thead><tr><th>#</th><th>PLAYER / SOURCE</th><th>ROLE</th><th>DRESSED</th><th>ON ICE</th></tr></thead><tbody>{[...groupNames].sort(([a], [b]) => a.localeCompare(b)).map(([key, name]) => <ImportRosterGroup key={key} label={name} players={roster.filter(player => playerGroup(player) === key)} locked={!!busy} onChange={changePlayer} onStartingChange={(id, checked) => { setStartingMode('manual'); changePlayer(id, { location: checked ? 'ON_ICE' : 'BENCH' }) }} />)}</tbody></table></div>
          <div className="import-lines"><div className="import-lines-heading"><h4><Layers3 size={16} />Forward lines & defensive pairs</h4><div><button className="text-button" onClick={() => setLines(previous => [...previous, { id: createUuid(), team_id: team.id, name: `Forward line ${teamLines.filter(line => line.kind === 'FORWARD').length + 1}`, kind: 'FORWARD', player_ids: [], slots: { LW: null, C: null, RW: null } }])}><Plus size={13} />Line</button><button className="text-button" onClick={() => setLines(previous => [...previous, { id: createUuid(), team_id: team.id, name: `Defensive pair ${teamLines.filter(line => line.kind === 'DEFENCE').length + 1}`, kind: 'DEFENCE', player_ids: [], slots: { LD: null, RD: null } }])}><Plus size={13} />Pair</button></div></div><p>Reassign players by slot. Empty slots are allowed. Source roster codes remain unchanged.</p>{teamLines.map(line => <div className="import-line" key={line.id}><div><label className="sr-only" htmlFor={`import-line-${line.id}`}>Preset name</label><input id={`import-line-${line.id}`} value={line.name} onChange={event => setLines(previous => previous.map(entry => entry.id === line.id ? { ...entry, name: event.target.value } : entry))} /><button className="icon-button" onClick={() => setLines(previous => previous.filter(entry => entry.id !== line.id))} aria-label={`Remove ${line.name}`}><X size={15} /></button></div><div className="import-line-slots">{slotNames(line.kind).map(slot => <label key={slot}>{slot}<select disabled={!!busy} value={slotsFor(line)[slot] || ''} onChange={event => setSlot(line.id, slot, event.target.value)}><option value="">Empty slot</option>{roster.map(player => <option key={player.id} value={player.id}>#{player.number} {player.name}{!player.eligible ? ' · unavailable' : ''}</option>)}</select></label>)}</div></div>)}</div>
        </section>
      })}</div>
      <div className="source-confirm"><div><CheckCircle2 size={20} /><span><strong>Ready to create the local tracking session?</strong><small>Both rosters, your selected lineup and presets are saved together. Existing sessions are resumed safely.</small></span></div><button className="primary-button" disabled={!!busy} onClick={() => void confirm()}>{busy === 'import' ? <LoaderCircle size={17} className="spin" /> : <FileCheck2 size={17} />}{busy === 'import' ? 'Preparing tracking session…' : 'Confirm import & open tracking'}<ArrowRight size={16} /></button></div>
    </>}
  </section>
}

function ImportRosterGroup({ label, players, locked, onChange, onStartingChange }: { label: string; players: ImportedPlayer[]; locked: boolean; onChange: (id: string, changes: Partial<ImportedPlayer>) => void; onStartingChange: (id: string, checked: boolean) => void }) {
  return <><tr className="import-roster-group"><th colSpan={5}>{label}</th></tr>{players.map(player => <tr key={player.id} className={!player.eligible ? 'not-dressed' : player.location === 'ON_ICE' ? 'starting-player' : ''}><td><input disabled={locked} aria-label={`Jersey number for ${player.name}`} inputMode="numeric" value={player.number} maxLength={2} onChange={event => onChange(player.id, { number: event.target.value.replace(/[^0-9]/g, '') })} /></td><td><input disabled={locked} aria-label={`Player name for number ${player.number}`} value={player.name} onChange={event => onChange(player.id, { name: event.target.value })} /><small title="Original external assignment is retained separately">{player.source.roster_code || 'Unknown code'} · {player.source.raw_position || 'Unknown position'}{player.source.starting_six ? ' · Official starter' : ''}</small></td><td><select disabled={locked} aria-label={`Assigned position for ${player.name}`} value={playerRole(player)} onChange={event => onChange(player.id, { assigned_role: event.target.value, position: rolePosition(event.target.value) })}><option value="" disabled>Choose role</option>{roles.map(role => <option key={role}>{role}</option>)}</select></td><td><label className="import-check"><input type="checkbox" disabled={locked || !playerRole(player)} title={!playerRole(player) ? 'Choose an assigned role before marking this player eligible' : undefined} aria-label={`${player.name} eligible`} checked={player.eligible} onChange={event => onChange(player.id, { eligible: event.target.checked, ...(!event.target.checked ? { location: 'BENCH' } : {}) })} /></label></td><td><label className="import-check"><input type="checkbox" disabled={locked || !player.eligible} aria-label={`${player.name} starts on ice`} checked={player.eligible && player.location === 'ON_ICE'} onChange={event => onStartingChange(player.id, event.target.checked)} /></label></td></tr>)}</>
}

export function SourceRefresh({ state, onApplied }: { state: Snapshot; onApplied: (snapshot: Snapshot) => void }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [diff, setDiff] = useState<RefreshDiff | null>(null)
  const [error, setError] = useState('')
  const [restoreLines, setRestoreLines] = useState(false)
  const [notice, setNotice] = useState('')
  if (state.integration?.provider !== 'hokejovy-zapis') return null
  const preview = async () => {
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await api<RefreshDiff>(`/api/v1/games/${state.game.id}/roster/refresh`, { method: 'POST', body: JSON.stringify({ request_id: createUuid(), expected_version: state.version, confirm: false, restore_lines: restoreLines }) }, 45000)
      setDiff(result); setOpen(true)
    } catch (e) { setError((e as Error).name === 'AbortError' ? 'The roster source timed out. Local tracking remains available. Try again when the source recovers.' : (e as Error).message); setOpen(true) }
    finally { setBusy(false) }
  }
  const apply = async () => {
    if (!diff) return
    setBusy(true); setError('')
    const body = JSON.stringify({ request_id: createUuid(), expected_version: diff.version, confirm: true, preview_fingerprint: diff.preview_fingerprint, restore_lines: restoreLines })
    try {
      let result: Snapshot
      try { result = await api<Snapshot>(`/api/v1/games/${state.game.id}/roster/refresh`, { method: 'POST', body }, 45000) }
      catch (e) { if ((e as { status?: number }).status) throw e; result = await api<Snapshot>(`/api/v1/games/${state.game.id}/roster/refresh`, { method: 'POST', body }, 45000) }
      onApplied(result); setDiff(null); setOpen(false); setNotice('Source refresh applied. Recorded shifts and player locations are preserved.'); setRestoreLines(false)
    } catch (e) { setError((e as Error).message); if ((e as { status?: number }).status === 409) setDiff(null) }
    finally { setBusy(false) }
  }
  return <div className="source-refresh"><div><span className="eyebrow"><Globe2 size={12} />HOKEJOVÝ ZÁPIS · #{state.integration.external_game_id}</span><h3>Connected source roster</h3><p>Compare the latest official metadata with this session. On-ice locations, TOI and saved operator corrections stay authoritative. Save any setup draft edits before applying a refresh.</p></div><button className="subtle-button" disabled={busy} onClick={() => void preview()}>{busy ? <LoaderCircle size={15} className="spin" /> : <RefreshCw size={15} />}Refresh roster from source</button>{notice && <p className="source-refresh-notice" role="status"><Check size={14} />{notice}</p>}{open && <div className="source-refresh-diff"><div><h4>Review source differences</h4><button className="icon-button" onClick={() => setOpen(false)} disabled={busy} aria-label="Close source comparison"><X size={17} /></button></div>{error && <div className="form-error" role="alert">{error}</div>}{diff && <><div className="refresh-change-list">{diff.changes.length ? diff.changes.map((change, index) => <div key={index}><span className="refresh-change-type">{change.type.replaceAll('_', ' ')}</span><span><strong>{change.message || `${change.player_id ? state.players.find(player => player.id === change.player_id)?.name || change.player_id : 'Match'}${change.field ? ` · ${change.field.replaceAll('_', ' ')}` : ''}`}</strong>{(change.before !== undefined || change.after !== undefined) && <small>{showValue(change.before)} <ArrowRight size={11} /> {showValue(change.after)}</small>}</span></div>) : <div className="no-source-changes"><CheckCircle2 size={17} />No source changes detected.</div>}</div>{!!diff.warnings.length && <div className="source-warnings"><ul>{diff.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div>}<label className="restore-lines-option"><input type="checkbox" checked={restoreLines} onChange={event => setRestoreLines(event.target.checked)} /><span><strong>Restore official line & pairing presets</strong><small>This replaces locally edited presets with the imported source configuration. Player locations and shifts are preserved.</small></span></label><div className="refresh-diff-actions"><span>Compared at match version {diff.version}</span><button disabled={busy} className="primary-button" onClick={() => void apply()}>{busy ? <LoaderCircle size={15} className="spin" /> : <Check size={15} />}{restoreLines ? 'Apply refresh & restore official presets' : 'Apply reviewed refresh'}</button></div></>}{!diff && <button disabled={busy} className="subtle-button" onClick={() => void preview()}><RefreshCw size={14} />Compare again</button>}</div>}</div>
}
