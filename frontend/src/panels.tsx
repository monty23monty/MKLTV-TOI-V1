import { createUuid } from './uuid'
import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Check, ChevronDown, ChevronUp, Clock3, Download, FileJson, Layers3, LoaderCircle, Plus, Save, Shield, Upload, Users, X } from 'lucide-react'
import type { Action, Line, Location, MatchEvent, Player, Position, Snapshot } from './types'
import { duration, eventDescription } from './App'
import { SourceRefresh } from './HokejovyImport'

export function ClockEditor({ state, action, locked, onClose, operatorMode = false }: { state: Snapshot; action: Action; locked: boolean; onClose: () => void; operatorMode?: boolean }) {
  const [period, setPeriod] = useState(state.clock.period)
  const [remaining, setRemaining] = useState(duration(state.clock.remaining_ms))
  const [lengths, setLengths] = useState(state.game.period_lengths_ms.map(length => length / 60000))
  const [overtimeMinutes, setOvertimeMinutes] = useState(5)
  const [overtimePrepared, setOvertimePrepared] = useState(false)
  const [error, setError] = useState('')
  const validLength = (length: number) => Number.isFinite(length) && length > 0 && length <= 120
  const canPrepareOvertime = period >= 3 && period === lengths.length && lengths.length < 10 && !overtimePrepared
  const prepareOvertime = () => {
    if (locked || !canPrepareOvertime) return
    if (!validLength(overtimeMinutes)) { setError('Overtime must be between 1 and 120 minutes.'); return }
    const nextLengths = [...lengths, overtimeMinutes]
    setLengths(nextLengths)
    setPeriod(nextLengths.length)
    setRemaining(duration(Math.round(overtimeMinutes * 60000)))
    setOvertimePrepared(true)
    setError('')
  }
  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    const match = remaining.match(/^(\d{1,3}):([0-5]\d)$/)
    if (!match) { setError('Use minutes:seconds, for example 12:35.'); return }
    const ms = (Number(match[1]) * 60 + Number(match[2])) * 1000
    if (lengths.some(length => !validLength(length))) { setError('Each period must be between 1 and 120 minutes.'); return }
    if (ms > lengths[period - 1] * 60000) { setError('Remaining time cannot exceed this period’s duration.'); return }
    if (await action('clock', { operation: 'set', period, remaining_ms: ms, period_lengths_ms: lengths.map(length => Math.round(length * 60000)) })) onClose()
  }
  return <div className="modal-backdrop"><form className="modal clock-modal" role="dialog" aria-modal="true" aria-label="Game clock controls" onSubmit={submit}><div className="modal-header"><div><span className="eyebrow">AUTHORITATIVE GAME TIME</span><h2>Clock controls</h2></div><button type="button" className="icon-button" onClick={onClose} aria-label="Close clock controls"><X /></button></div>{!operatorMode && <p className="muted">Clock corrections change the scoreboard display. Recorded playing time and shifts remain continuous.</p>}<div className="form-row"><label>Current period<select value={period} onChange={event => { const next = Number(event.target.value); setPeriod(next); setRemaining(duration(lengths[next - 1] * 60000)) }}>{lengths.map((_, index) => <option key={index} value={index + 1}>{index < 3 ? `Period ${index + 1}` : `Overtime ${index - 2}`}</option>)}</select></label><label>Remaining time<input autoFocus value={remaining} onChange={event => setRemaining(event.target.value)} placeholder="20:00" inputMode="numeric" /></label></div>{operatorMode && canPrepareOvertime && <div className="form-row"><label>Overtime minutes<input type="number" min="1" max="120" step="1" value={overtimeMinutes} disabled={locked} onChange={event => setOvertimeMinutes(Number(event.target.value))} /></label><button type="button" className="subtle-button" disabled={locked} onClick={prepareOvertime}><Plus size={16} />Prepare overtime</button></div>}{!operatorMode && <div className="period-config"><span className="eyebrow">PERIOD LENGTHS · MINUTES</span><div>{lengths.map((length, index) => <label key={index}>P{index + 1}<input type="number" min="1" max="120" step="1" value={length} onChange={event => setLengths(previous => previous.map((item, i) => i === index ? Number(event.target.value) : item))} /></label>)}</div><button type="button" className="text-button" disabled={lengths.length >= 10} onClick={() => setLengths(previous => [...previous, 5])}><Plus size={14} />Add overtime period</button></div>}{error && <div role="alert" className="form-error">{error}</div>}<div className="modal-actions"><button type="button" className="subtle-button" disabled={locked || state.clock.period >= state.game.period_lengths_ms.length} onClick={async () => { if (await action('clock', { operation: 'advance' })) onClose() }}>Advance period<ArrowRight size={15} /></button><button className="primary-button" disabled={locked}><Check size={16} />Apply clock</button></div></form></div>
}

type RosterDraft = Pick<Player, 'id' | 'team_id' | 'name' | 'number' | 'position' | 'eligible' | 'location'>
const rosterDraft = (players: Player[]): RosterDraft[] => players.map(({ id, team_id, name, number, position, eligible, location }) => ({ id, team_id, name, number, position, eligible, location }))

export function SetupPanel({ state, action, locked, onDone, onSnapshot }: { state: Snapshot; action: Action; locked: boolean; onDone: () => void; onSnapshot: (snapshot: Snapshot) => void }) {
  const [players, setPlayers] = useState<RosterDraft[]>(() => rosterDraft(state.players))
  const [lines, setLines] = useState<Line[]>(() => structuredClone(state.lines))
  const [gameName, setGameName] = useState(state.game.name)
  const [competition, setCompetition] = useState(state.game.competition)
  const [teamNames, setTeamNames] = useState<Record<string, string>>(() => Object.fromEntries(state.teams.map(team => [team.id, team.name])))
  const [error, setError] = useState('')
  const [importOpen, setImportOpen] = useState(false)
  const [importText, setImportText] = useState('')
  const [importTeam, setImportTeam] = useState(state.teams[0]?.id)
  const [expandedLine, setExpandedLine] = useState<string | null>(null)
  const [token, setToken] = useState(() => sessionStorage.getItem('toi-operator-token') || '')
  const fileInput = useRef<HTMLInputElement>(null)
  const loadedGame = useRef(state.game.id)
  const trackingBegun = state.clock.playing_ms > 0 || state.events.some(event => ['move', 'substitute'].includes(event.type))
  useEffect(() => {
    if (loadedGame.current === state.game.id) return
    loadedGame.current = state.game.id
    setPlayers(rosterDraft(state.players)); setLines(structuredClone(state.lines)); setGameName(state.game.name); setCompetition(state.game.competition); setTeamNames(Object.fromEntries(state.teams.map(team => [team.id, team.name])))
  }, [state])
  const updatePlayer = (id: string, changes: Partial<RosterDraft>) => setPlayers(previous => previous.map(player => player.id === id ? { ...player, ...changes } : player))
  const save = async () => {
    setError('')
    if (!gameName.trim() || players.some(player => !player.name.trim() || !String(player.number).trim())) { setError('Match name, player names and jersey numbers are required.'); return }
    if (players.some(player => !Number.isInteger(Number(player.number)) || Number(player.number) < 0 || Number(player.number) > 99)) { setError('Jersey numbers must be whole numbers between 0 and 99.'); return }
    for (const team of state.teams) {
      const dressed = players.filter(player => player.team_id === team.id && player.eligible)
      if (new Set(dressed.map(player => String(player.number))).size !== dressed.length) { setError(`${teamNames[team.id]} has duplicate dressed jersey numbers.`); return }
    }
    if (lines.some(line => !line.name.trim() || !line.player_ids.length)) { setError('Each line needs a name and at least one player. Remove empty lines or select members.'); return }
    const home = state.teams.find(team => team.side === 'home')
    const away = state.teams.find(team => team.side === 'away')
    const savedPlayers = players.map(player => {
      const { location, ...metadata } = player
      return { ...metadata, number: Number(player.number), ...(!trackingBegun ? { location } : {}) }
    })
    const ok = await action('setup', { players: savedPlayers, lines, game: { name: gameName.trim(), competition, home_name: home && teamNames[home.id], away_name: away && teamNames[away.id] } })
    if (ok) onDone()
  }
  const importRoster = () => {
    try {
      const parsed = JSON.parse(importText)
      const entries = Array.isArray(parsed) ? parsed : parsed.players
      if (!Array.isArray(entries) || !entries.length) throw new Error('Provide a non-empty players array or {"players": [...]} object.')
      const imported: RosterDraft[] = entries.map((entry: Record<string, unknown>) => {
        const teamId = String(entry.team_id || importTeam)
        if (!state.teams.some(team => team.id === teamId)) throw new Error(`Unknown team_id: ${teamId}. Use a team ID from the current match or omit it to use the selected team.`)
        const position = String(entry.position || 'C') as Position
        const location = String(entry.location || 'BENCH') as Location
        if (!['C', 'LW', 'RW', 'D', 'G'].includes(position)) throw new Error('Position must be C, LW, RW, D or G.')
        if (!['ON_ICE', 'BENCH', 'PENALTY_BOX'].includes(location)) throw new Error('Location must be ON_ICE, BENCH or PENALTY_BOX.')
        if (!entry.name || entry.number === undefined) throw new Error('Each player needs a name and number.')
        const existing = players.find(player => player.id === entry.id) || players.find(player => player.team_id === teamId && String(player.number) === String(entry.number))
        if (trackingBegun && !existing) throw new Error('Add new roster players before tracking begins. Existing player metadata can still be imported.')
        return { id: String(entry.id || existing?.id || createUuid()), team_id: teamId, name: String(entry.name), number: String(entry.number), position, eligible: entry.eligible !== false, location: entry.eligible === false ? 'BENCH' : location }
      })
      setPlayers(previous => [...previous.filter(player => !imported.some(entry => entry.id === player.id)), ...imported])
      if (Array.isArray(parsed.lines)) setLines(previous => [...previous.filter(line => !parsed.lines.some((entry: Line) => entry.id === line.id)), ...parsed.lines.map((line: Line) => ({ ...line, id: line.id || createUuid(), team_id: line.team_id || importTeam }))])
      setImportOpen(false); setImportText(''); setError('')
    } catch (e) { setError(`Roster import: ${(e as Error).message}`) }
  }
  const exportRoster = () => {
    const blob = new Blob([JSON.stringify({ game: { name: gameName, competition }, teams: state.teams, players, lines }, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = `${state.game.id}-roster.json`; link.click(); URL.revokeObjectURL(url)
  }
  return <section className="setup-panel"><div className="setup-intro"><div><h2>Match configuration</h2><p>Prepare your roster, opening lineup and quick-change presets. Once tracking begins, correct player locations using the live movement controls.</p></div><div><button className="subtle-button" onClick={exportRoster}><Download size={15} />Export roster</button><button className="subtle-button" onClick={() => setImportOpen(!importOpen)}><Upload size={15} />Import JSON</button><button disabled={locked} className="primary-button" onClick={() => void save()}>{locked ? <LoaderCircle size={16} className="spin" /> : <Save size={16} />}Save & track</button></div></div>{error && <div role="alert" className="form-error">{error}</div>}
    <SourceRefresh state={state} onApplied={snapshot => { onSnapshot(snapshot); setPlayers(rosterDraft(snapshot.players)); setLines(structuredClone(snapshot.lines)); setGameName(snapshot.game.name); setCompetition(snapshot.game.competition); setTeamNames(Object.fromEntries(snapshot.teams.map(team => [team.id, team.name]))) }} /><div className="setup-game-fields"><label>Match name<input value={gameName} onChange={event => setGameName(event.target.value)} /></label><label>Competition<input value={competition} onChange={event => setCompetition(event.target.value)} /></label><div className="setup-clock-note"><Clock3 size={18} /><span>Period lengths and scoreboard time are configured in the clock controls above.</span></div></div>
    {importOpen && <div className="import-panel"><div><h3><FileJson size={17} />Import game roster</h3><button className="icon-button" onClick={() => setImportOpen(false)} aria-label="Close import"><X size={18} /></button></div><p>Import merges by player ID or team and jersey number. Omit IDs for new players. Existing unlisted players remain; mark unused players unavailable.</p><label>Default team<select value={importTeam} onChange={event => setImportTeam(event.target.value)}>{state.teams.map(team => <option key={team.id} value={team.id}>{teamNames[team.id]}</option>)}</select></label><textarea rows={5} value={importText} onChange={event => setImportText(event.target.value)} placeholder={'[{"name":"Alex Morgan","number":17,"position":"C","eligible":true,"location":"BENCH"}]'} /><input type="file" ref={fileInput} accept="application/json,.json" hidden onChange={event => { const file = event.target.files?.[0]; if (file) void file.text().then(setImportText) }} /><div><button className="subtle-button" onClick={() => fileInput.current?.click()}><Upload size={15} />Choose JSON file</button><button className="primary-button" disabled={!importText.trim()} onClick={importRoster}><Check size={15} />Apply to draft</button></div></div>}
    <div className="setup-teams">{state.teams.map(team => {
      const roster = players.filter(player => player.team_id === team.id)
      return <div className="roster-editor" key={team.id}><div className="roster-header"><span className="eyebrow">{team.side.toUpperCase()} TEAM</span><label className="sr-only" htmlFor={`team-name-${team.id}`}>Team name</label><input id={`team-name-${team.id}`} value={teamNames[team.id]} onChange={event => setTeamNames(previous => ({ ...previous, [team.id]: event.target.value }))} /><span>{roster.filter(player => player.eligible && player.location === 'ON_ICE').length} on ice · {roster.filter(player => player.eligible).length} dressed</span></div><div className="roster-scroll"><table className="roster-table"><thead><tr><th>#</th><th>PLAYER NAME</th><th>POS</th><th>LOCATION</th><th>DRESSED</th></tr></thead><tbody>{roster.map(player => <tr key={player.id} className={!player.eligible ? 'not-dressed' : ''}><td><input aria-label={`Jersey number for ${player.name}`} value={player.number} inputMode="numeric" maxLength={3} onChange={event => updatePlayer(player.id, { number: event.target.value.replace(/[^0-9]/g, '') })} /></td><td><input aria-label={`Name for number ${player.number}`} value={player.name} onChange={event => updatePlayer(player.id, { name: event.target.value })} /></td><td><select aria-label={`Position for ${player.name}`} value={player.position} onChange={event => updatePlayer(player.id, { position: event.target.value as Position })}>{['LW', 'C', 'RW', 'D', 'G'].map(position => <option key={position}>{position}</option>)}</select></td><td><select disabled={!player.eligible || trackingBegun} aria-label={`Starting location for ${player.name}`} value={player.location} onChange={event => updatePlayer(player.id, { location: event.target.value as Location })}><option value="ON_ICE">On ice</option><option value="BENCH">Bench</option><option value="PENALTY_BOX">Penalty box</option></select></td><td><label className="eligibility"><input type="checkbox" aria-label={`${player.name} dressed and eligible`} checked={player.eligible} disabled={trackingBegun && state.players.find(entry => entry.id === player.id)?.location !== 'BENCH'} onChange={event => updatePlayer(player.id, { eligible: event.target.checked, ...(!event.target.checked ? { location: 'BENCH' } : {}) })} /><span /></label></td></tr>)}</tbody></table></div><button disabled={trackingBegun} title={trackingBegun ? 'New roster players must be added before tracking begins' : 'Add player'} className="add-player" onClick={() => setPlayers(previous => [...previous, { id: createUuid(), team_id: team.id, name: '', number: '', position: 'C', eligible: true, location: 'BENCH' }])}><Plus size={15} />Add roster player</button><div className="line-editor"><div className="line-editor-heading"><h3><Layers3 size={16} />Line & pairing presets</h3><button className="text-button" onClick={() => { const id = createUuid(); setLines(previous => [...previous, { id, team_id: team.id, name: 'New line', kind: 'FORWARD', player_ids: [] }]); setExpandedLine(id) }}><Plus size={15} />Add</button></div>{lines.filter(line => line.team_id === team.id).map(line => <div key={line.id} className="line-edit-row"><div><button className="line-expand" aria-expanded={expandedLine === line.id} onClick={() => setExpandedLine(expandedLine === line.id ? null : line.id)}>{line.kind === 'DEFENCE' ? <Shield size={14} /> : <Users size={14} />}<strong>{line.name}</strong><span>{line.player_ids.map(id => `#${roster.find(player => player.id === id)?.number || '?'}`).join(' · ') || 'Choose players'}</span>{expandedLine === line.id ? <ChevronUp size={14} /> : <ChevronDown size={14} />}</button><button className="icon-button" onClick={() => setLines(previous => previous.filter(entry => entry.id !== line.id))} aria-label={`Remove ${line.name}`}><X size={14} /></button></div>{expandedLine === line.id && <div className="line-details"><div className="form-row"><label>Preset name<input value={line.name} onChange={event => setLines(previous => previous.map(entry => entry.id === line.id ? { ...entry, name: event.target.value } : entry))} /></label><label>Type<select value={line.kind} onChange={event => setLines(previous => previous.map(entry => entry.id === line.id ? { ...entry, kind: event.target.value as Line['kind'] } : entry))}><option value="FORWARD">Forward line</option><option value="DEFENCE">Defensive pair</option><option value="SPECIAL">Special combination</option></select></label></div><div className="line-player-select">{roster.map(player => <label key={player.id}><input type="checkbox" checked={line.player_ids.includes(player.id)} onChange={event => setLines(previous => previous.map(entry => entry.id === line.id ? { ...entry, player_ids: event.target.checked ? [...entry.player_ids, player.id] : entry.player_ids.filter(id => id !== player.id) } : entry))} /><span><strong>#{player.number}</strong>{player.name.split(' ').slice(-1)[0]}<small>{player.position}</small></span></label>)}</div></div>}</div>)}</div></div>
    })}</div><div className="operator-access"><div><h3>Operator access</h3><p>If the deployment requires authentication, save a server-issued token in this browser. Tokens are never exported with match data.</p></div><label>Bearer token<input type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} placeholder="Optional for local prototype" /></label><button className="subtle-button" onClick={() => { sessionStorage.setItem('toi-operator-token', token.trim()); location.reload() }}>Save & reconnect</button></div><div className="setup-bottom"><p><Check size={15} />All starting locations and presets are saved together.</p><button disabled={locked} className="primary-button" onClick={() => void save()}><Save size={16} />Save configuration & return to live tracking</button></div></section>
}

export function CorrectionPanel({ state, event, action, locked, onClose }: { state: Snapshot; event: MatchEvent; action: Action; locked: boolean; onClose: () => void }) {
  const teamId = String(event.payload.team_id || state.players.find(player => player.id === event.payload.player_id)?.team_id || state.teams[0].id)
  const [outgoing, setOutgoing] = useState<string[]>(event.payload.outgoing_ids as string[] || [])
  const [incoming, setIncoming] = useState<string[]>(event.payload.incoming_ids as string[] || [])
  const [playerId, setPlayerId] = useState(String(event.payload.player_id || ''))
  const [location, setLocation] = useState<Location>((event.payload.to as Location) || 'BENCH')
  const [error, setError] = useState('')
  const players = state.players.filter(player => player.team_id === teamId)
  const submit = async () => {
    if (event.type === 'substitute' && (!outgoing.length || !incoming.length || outgoing.some(id => incoming.includes(id)))) { setError('Choose distinct outgoing and incoming players.'); return }
    const payload = event.type === 'move' ? { player_id: playerId, to: location } : { team_id: teamId, outgoing_ids: outgoing, incoming_ids: incoming }
    if (await action('correct', { event_id: event.id, replacement: { type: event.type, payload } })) onClose()
  }
  return <div className="modal-backdrop"><section className="modal correction-modal" role="dialog" aria-modal="true" aria-label="Correct historical movement"><div className="modal-header"><div><span className="eyebrow">P{event.period} · {duration(event.remaining_ms)} · AUDITABLE CORRECTION</span><h2>Correct this movement</h2></div><button className="icon-button" onClick={onClose} aria-label="Close correction"><X /></button></div><p className="correction-original">Recorded: <strong>{eventDescription(event, state)}</strong></p><p className="muted">The original timestamp is preserved. Shifts and learned replacements are rebuilt from the corrected history. Later conflicting movements may prevent this correction.</p>{event.type === 'move' ? <div className="form-row"><label>Player<select value={playerId} onChange={change => setPlayerId(change.target.value)}>{players.map(player => <option key={player.id} value={player.id}>#{player.number} {player.name}</option>)}</select></label><label>Destination<select value={location} onChange={change => setLocation(change.target.value as Location)}><option value="ON_ICE">On ice</option><option value="BENCH">Bench</option><option value="PENALTY_BOX">Penalty box</option></select></label></div> : <div className="correction-groups">{([{ label: 'OUTGOING', values: outgoing, setter: setOutgoing }, { label: 'INCOMING', values: incoming, setter: setIncoming }]).map(group => <div key={group.label}><span className="eyebrow">{group.label}</span><div className="correction-player-grid">{players.map(player => <button key={player.id} className={group.values.includes(player.id) ? 'selected' : ''} onClick={() => group.setter(previous => previous.includes(player.id) ? previous.filter(id => id !== player.id) : [...previous, player.id])}><strong>{player.number}</strong><span>{player.name.split(' ').slice(-1)[0]}</span></button>)}</div></div>)}</div>}{error && <div role="alert" className="form-error">{error}</div>}<div className="modal-actions"><button className="subtle-button" onClick={onClose}>Cancel</button><button className="primary-button" disabled={locked} onClick={() => void submit()}><Check size={16} />Save correction</button></div></section></div>
}
