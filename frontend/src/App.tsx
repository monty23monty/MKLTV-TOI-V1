import { useEffect, useRef, useState } from 'react'
import { Activity, ArrowDownToLine, ArrowLeftRight, ArrowRight, Check, ChevronDown, CircleHelp, Clock3, Download, ExternalLink, History, Layers3, LoaderCircle, Maximize2, Minimize2, Pause, Play, Plus, RotateCcw, Settings2, ShieldAlert, SlidersHorizontal, Sparkles, Square, Users, WifiOff, X, Zap } from 'lucide-react'
import type { Line, Location, MatchEvent, Player, Snapshot, Suggestions, Team } from './types'
import { api, useGame } from './useGame'
import { SetupPanel, ClockEditor, CorrectionPanel } from './panels'
import { HokejovyImport } from './HokejovyImport'
import { useOperatorMode } from './useOperatorMode'
import './operator.css'

export function duration(ms: number) {
  const seconds = Math.max(0, Math.floor(ms / 1000))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}
const positionOrder = { LW: 0, C: 1, RW: 2, D: 3, G: 4 }
const orderPlayers = (players: Player[]) => [...players].sort((a, b) => positionOrder[a.position] - positionOrder[b.position] || Number(a.number) - Number(b.number))
type Selection = { teamId: string; outgoing: string[]; incoming: string[]; at: number }

export default function App() {
  const rootRef = useRef<HTMLDivElement>(null)
  const operator = useOperatorMode(rootRef)
  const game = useGame()
  const { state, connection, pending, action, elapsed } = game
  const [view, setView] = useState<'live' | 'stats' | 'setup' | 'import' | 'history'>('live')
  const [selection, setSelection] = useState<Selection | null>(null)
  const [operationRevision, setOperationRevision] = useState(0)
  const [focused, setFocused] = useState<string | null>(null)
  const [multi, setMulti] = useState(false)
  const [suggestionResult, setSuggestionResult] = useState<{ key: string; data: Suggestions } | null>(null)
  const [clockEditor, setClockEditor] = useState(false)
  const [correction, setCorrection] = useState<MatchEvent | null>(null)
  const [help, setHelp] = useState(false)
  const [compact, setCompact] = useState(() => localStorage.getItem('toi-compact') === 'true')
  const [showGames, setShowGames] = useState(false)
  const [newGame, setNewGame] = useState(false)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [homeName, setHomeName] = useState('Milton Keynes Thunderbolts')
  const [awayName, setAwayName] = useState('Sheffield Steelhawks')
  const [toast, setToast] = useState('')
  const [accessToken, setAccessToken] = useState('')
  const [suggestionFailure, setSuggestionFailure] = useState<string | null>(null)
  const suggestionKey = state && selection?.outgoing.length ? JSON.stringify([state.game.id, selection.teamId, selection.outgoing, state.version]) : null
  const suggestions = suggestionResult?.key === suggestionKey ? suggestionResult?.data ?? null : null
  const suggestionError = suggestionKey !== null && suggestionFailure === suggestionKey
  const lastGame = useRef(game.gameId)
  const locked = pending || connection !== 'live'

  useEffect(() => {
    if (lastGame.current !== game.gameId) { setSelection(null); setFocused(null); setSuggestionResult(null); lastGame.current = game.gameId }
  }, [game.gameId])
  useEffect(() => {
    if (!suggestionKey || !selection || !state) return
    let active = true
    api<Suggestions>(`/api/v1/games/${state.game.id}/suggestions?team_id=${encodeURIComponent(selection.teamId)}&outgoing_ids=${selection.outgoing.map(encodeURIComponent).join(',')}`)
      .then(result => { if (active) { setSuggestionResult({ key: suggestionKey, data: result }); setSuggestionFailure(null) } }).catch(() => { if (active) { setSuggestionResult(null); setSuggestionFailure(suggestionKey) } })
    return () => { active = false }
  }, [suggestionKey])
  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(''), 3500)
    return () => window.clearTimeout(timer)
  }, [toast])
  useEffect(() => {
    if (!clockEditor && !correction && !help && !newGame) return
    const previousFocus = document.activeElement as HTMLElement | null
    const modal = document.querySelector<HTMLElement>('.modal')
    const selector = 'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href]'
    const focusable = () => Array.from(modal?.querySelectorAll<HTMLElement>(selector) || []).filter(element => element.offsetParent !== null)
    focusable()[0]?.focus()
    const trap = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return
      const controls = focusable()
      if (!controls.length) return
      if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1)?.focus() }
      else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0].focus() }
    }
    window.addEventListener('keydown', trap)
    return () => { window.removeEventListener('keydown', trap); if (previousFocus?.isConnected) previousFocus.focus() }
  }, [clockEditor, correction, help, newGame])
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { if (!operator.active) { setSelection(null); setFocused(null) }; setCorrection(null); setClockEditor(false); setHelp(false); setShowGames(false); setNewGame(false) }
      if ((event.ctrlKey || event.metaKey) && event.key === 'z' && !(event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement)) {
        event.preventDefault()
        void action('undo', {}).then(saved => { if (saved) { setSelection(null); setFocused(null); setOperationRevision(value => value + 1) } })
      }
    }
    window.addEventListener('keydown', keydown)
    return () => window.removeEventListener('keydown', keydown)
  }, [action, operator.active])

  const notifyAction = async (type: string, payload: Record<string, unknown>, message?: string) => {
    const saved = await action(type, payload)
    if (saved) { setSelection(null); setFocused(null); setOperationRevision(value => value + 1) }
    if (saved && message) setToast(message)
    return saved
  }
  const executeSwap = async (outgoing: string[], incoming: string[], teamId: string, at: number) => {
    await notifyAction('substitute', { team_id: teamId, outgoing_ids: outgoing, incoming_ids: incoming, at_playing_ms: Math.floor(at) }, 'Lineup updated')
  }
  const tapPlayer = (player: Player) => {
    if (!state || locked || !player.eligible) return
    if (player.location === 'PENALTY_BOX') { setFocused(focused === player.id ? null : player.id); setSelection(null); return }
    if (player.location === 'ON_ICE') {
      setFocused(player.id)
      setSelection(previous => {
        if (previous?.teamId === player.team_id && multi) {
          const outgoing = previous.outgoing.includes(player.id) ? previous.outgoing.filter(id => id !== player.id) : [...previous.outgoing, player.id]
          return outgoing.length ? { ...previous, outgoing, incoming: [] } : null
        }
        if (previous?.outgoing[0] === player.id && !multi) { setFocused(null); return null }
        return { teamId: player.team_id, outgoing: [player.id], incoming: [], at: state.clock.playing_ms + elapsed }
      })
    } else if (selection?.teamId === player.team_id && !selection.outgoing.length) {
      void notifyAction('move', { player_id: player.id, to: 'ON_ICE' }, 'Player added to ice')
    } else if (selection?.teamId === player.team_id && selection.outgoing.length) {
      if (!multi && selection.outgoing.length === 1) void executeSwap(selection.outgoing, [player.id], player.team_id, selection.at)
      else setSelection(previous => previous && ({ ...previous, incoming: previous.incoming.includes(player.id) ? previous.incoming.filter(id => id !== player.id) : [...previous.incoming, player.id] }))
    } else { setSelection(null); setFocused(focused === player.id ? null : player.id) }
  }
  const selectGroup = (teamId: string, kind: 'FORWARD' | 'DEFENCE' | 'ALL') => {
    if (!state) return
    const outgoing = state.players.filter(player => player.team_id === teamId && player.location === 'ON_ICE' && player.position !== 'G' && (kind === 'ALL' || (kind === 'DEFENCE' ? player.position === 'D' : player.position !== 'D'))).map(player => player.id)
    setSelection(outgoing.length ? { teamId, outgoing, incoming: [], at: state.clock.playing_ms + elapsed } : null)
    setFocused(null)
  }
  const chooseLine = (line: Line) => {
    if (!state || locked) return
    const selected = selection?.teamId === line.team_id ? selection : null
    const linePlayers = state.players.filter(player => line.player_ids.includes(player.id))
    if (linePlayers.some(player => !player.eligible || player.location === 'PENALTY_BOX')) return
    const incoming = linePlayers.filter(player => player.location === 'BENCH').map(player => player.id)
    const outgoing = state.players.filter(player => player.team_id === line.team_id && player.location === 'ON_ICE' && !line.player_ids.includes(player.id) && player.position !== 'G' && (line.kind === 'SPECIAL' || (line.kind === 'DEFENCE' ? player.position === 'D' : player.position !== 'D'))).map(player => player.id)
    if (incoming.length && outgoing.length) void executeSwap(outgoing, incoming, line.team_id, selected?.at ?? state.clock.playing_ms + elapsed)
  }
  const download = () => {
    if (!state) return
    const url = URL.createObjectURL(new Blob([JSON.stringify(state, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a'); link.href = url; link.download = `${state.game.id}-toi.json`; link.click(); URL.revokeObjectURL(url)
  }

  const liveWarnings = state?.warnings.filter(warning => /(?:skaters on ice|multiple goalkeepers on ice|empty net|skaters plus goalkeeper|clock feed is stale|playing time is paused)/i.test(warning)) || []
  return <div id="lightning-toi-operator" data-testid="operator-root" ref={rootRef} data-operator-mode={operator.mode} className={`app ${view === 'live' ? 'live-view' : 'admin-view'} ${compact ? 'compact' : ''} ${operator.active ? 'operator-mode' : ''}`}>
    {!operator.active && <header className="app-header">
      <a className="brand" href="#" aria-label="Lightning TV home" onClick={event => { event.preventDefault(); setView('live') }}><Zap fill="currentColor" size={23} /><span>LIGHTNING<span className="brand-tv">TV</span></span></a>
      <nav aria-label="Main navigation"><button className={view === 'live' ? 'active' : ''} onClick={() => setView('live')}><Activity size={16} />Live</button><button className={view === 'history' ? 'active' : ''} onClick={() => setView('history')}><History size={16} />History</button><button className={view === 'stats' ? 'active' : ''} onClick={() => setView('stats')}><Layers3 size={16} />Statistics</button><button className={view === 'setup' ? 'active' : ''} onClick={() => setView('setup')}><Settings2 size={16} />Match setup</button></nav>
      <div className="header-right"><div className="game-picker"><button onClick={() => setShowGames(!showGames)} disabled={pending} aria-label="Choose match" aria-expanded={showGames}><strong>{state?.game.name || 'Loading match…'}</strong><ChevronDown size={16} /></button>{showGames && <div className="game-menu"><span className="eyebrow">SAVED MATCHES</span>{game.games.map(match => <button key={match.id} onClick={() => { game.selectGame(match.id); setShowGames(false) }}><span>{match.name}<small>{match.status}</small></span>{match.id === game.gameId && <Check size={16} />}</button>)}<button className="gold-text" onClick={() => { setView('import'); setShowGames(false); setSelection(null); setFocused(null) }}><Download size={16} />Import from Hokejový Zápis</button><button className="gold-text" onClick={() => { setNewGame(true); setShowGames(false) }}><Plus size={16} />Create independent match</button></div>}</div><button className="icon-button" onClick={() => setHelp(true)} aria-label="Operator guide"><CircleHelp size={20} /></button></div>
    </header>}
    <main>
      {view !== 'live' && <div className="page-title"><h1>{view === 'stats' ? 'Player time & shift statistics' : view === 'history' ? 'Match history & corrections' : view === 'import' ? 'Import official match & roster' : 'Match setup & starting lineup'}</h1><button className="subtle-button" onClick={() => setView('live')}><ArrowRight size={16} />Return to live</button></div>}
      {view === 'live' && <div className="operator-controlbar" aria-label="Live match controls"><div className="operator-clock"><span className="operator-period">P<strong>{state?.clock.period ?? '—'}</strong></span><button className={`operator-time ${state?.clock.running ? 'running' : ''}`} onClick={() => setClockEditor(true)} disabled={!state || locked} aria-label="Edit period and game clock"><strong>{state ? duration(Math.ceil(Math.max(0, state.clock.remaining_ms - elapsed) / 1000) * 1000) : '--:--'}</strong><span>{state?.clock_authority?.mode === 'external' ? (state.clock_authority.feed_status === 'stale' ? 'Clock feed stale' : state.clock_authority.source) : state?.clock.running ? 'Running' : 'Stopped'}</span></button><button className="clock-toggle" disabled={!state || locked} onClick={() => void action('clock', { operation: state?.clock.running ? 'pause' : 'start' })}>{state?.clock.running ? <Pause size={19} fill="currentColor" /> : <Play size={19} fill="currentColor" />}<span>{state?.clock.running ? 'Pause' : 'Start clock'}</span></button></div><div className="operator-actions"><button className={`mode-button ${multi ? 'selected' : ''}`} aria-pressed={multi} onClick={() => { setMulti(!multi); setSelection(null); setFocused(null) }} disabled={locked}><Users size={16} />Group select</button><button className="undo-button" disabled={!state || locked || !state.events.some(event => !event.voided && ['substitute', 'move'].includes(event.type))} onClick={() => void notifyAction('undo', {}, 'Last change undone')}><RotateCcw size={18} />Undo</button>{!operator.active && <button className="icon-button density-button" onClick={() => setCompact(value => { localStorage.setItem('toi-compact', String(!value)); return !value })} aria-label={compact ? 'Use spacious layout' : 'Use compact layout'} title={compact ? 'Use spacious layout' : 'Use compact layout'}><SlidersHorizontal size={18} /></button>}{operator.active ? <button data-testid="operator-exit" className="subtle-button operator-fullscreen" onClick={() => void operator.exit()}><Minimize2 size={18} />{operator.browserFullscreen ? 'Exit Full Screen' : 'Exit focus layout'}</button> : <button data-testid="operator-enter" className="subtle-button operator-fullscreen" onClick={() => { setView('live'); setShowGames(false); void operator.enter() }}><Maximize2 size={18} />Operator Full Screen</button>}</div></div>}
      {operator.active && operator.fallbackReason && <div className="focus-layout-note" role="status">{operator.fallbackReason}</div>}
      {game.error && <div role="alert" className="error-banner"><ShieldAlert size={18} /><span>{game.error}</span><button aria-label="Dismiss error" onClick={() => game.setError('')}><X size={18} /></button></div>}
      {connection !== 'live' && state && <div className="warning-banner" role="status"><WifiOff size={17} />{connection === 'offline' ? 'Connection lost. Player controls paused while reconnecting.' : 'Synchronising match. Player controls paused.'}</div>}
      {view === 'live' && liveWarnings.length > 0 && <div className="warnings"><ShieldAlert size={15} /><span>{liveWarnings.join(' · ')}</span></div>}
      {view === 'setup' && state && state.warnings.length > 0 && <div className="warnings"><ShieldAlert size={15} /><span>{state.warnings.join(' · ')}</span></div>}
      {pending && <div className="saving-indicator" role="status"><LoaderCircle className="spin" size={14} />Saving</div>}
      {!state ? <div className="loading-state"><LoaderCircle className="spin" size={28} /><h2>Connecting to the rink</h2><p>Loading the authoritative match state.</p><button onClick={() => location.reload()} className="subtle-button">Retry connection</button><form className="access-form" onSubmit={event => { event.preventDefault(); sessionStorage.setItem('toi-operator-token', accessToken.trim()); location.reload() }}><label>Operator token, if required<input type="password" value={accessToken} onChange={event => setAccessToken(event.target.value)} autoComplete="off" placeholder="Server-issued access token" /></label><button className="subtle-button" disabled={!accessToken.trim()}>Connect with token</button></form></div> : <>
        {view === 'live' && <div className="teams-grid" data-testid="live-team-board">{state.teams.map(team => <TeamPanel key={team.id} team={team} state={state} selection={selection?.teamId === team.id ? selection : null} focused={focused} suggestions={selection?.teamId === team.id ? suggestions : null} suggestionError={selection?.teamId === team.id && suggestionError} operationRevision={operationRevision} locked={locked} onPlayer={tapPlayer} onGroup={kind => selectGroup(team.id, kind)} onLine={chooseLine} onAdd={() => { setFocused(null); setSelection({ teamId: team.id, outgoing: [], incoming: [], at: state.clock.playing_ms + elapsed }) }} onMove={(id, to) => void notifyAction('move', { player_id: id, to }, 'Player location updated')} onCancel={() => { setSelection(null); setFocused(null) }} onConfirm={() => selection && void executeSwap(selection.outgoing, selection.incoming, selection.teamId, selection.at)} onSuggestionGroup={ids => selection && suggestions?.groups.some(group => group.player_ids.length === ids.length && group.player_ids.every((id, index) => id === ids[index])) && void executeSwap(selection.outgoing, ids, team.id, selection.at)} />)}</div>}
        {view === 'history' && <RecentActivity state={state} locked={locked} onCorrect={setCorrection} onUndo={id => void notifyAction('undo', { event_id: id }, 'Movement undone')} />}
        {view === 'stats' && <Statistics state={state} elapsed={elapsed} onExport={download} />}
        {view === 'import' && <HokejovyImport onCancel={() => setView('live')} onImported={async snapshot => { await game.acceptImported(snapshot); setView('live'); setToast('Imported match ready') }} />}
        {view === 'setup' && <SetupPanel state={state} action={action} locked={locked} onSnapshot={game.applySnapshot} onDone={() => { setView('live'); setToast('Match configuration saved') }} />}
      </>}
      {view !== 'live' && <footer><span><Zap size={13} />LIGHTNING TV · TIME ON ICE</span><a href={`${import.meta.env.VITE_API_BASE_URL || ''}/docs`} target="_blank" rel="noreferrer">API documentation <ExternalLink size={11} /></a></footer>}
    </main>

    {toast && <div className="toast" role="status"><Check size={18} />{toast}</div>}
    {state && clockEditor && <ClockEditor operatorMode={operator.active} state={state} action={action} locked={locked} onClose={() => setClockEditor(false)} />}
    {state && correction && <CorrectionPanel state={state} event={correction} action={action} locked={locked} onClose={() => setCorrection(null)} />}
    {help && <div className="modal-backdrop" onClick={() => setHelp(false)}><section className="modal help-modal" role="dialog" aria-modal="true" aria-label="Operator guide" onClick={event => event.stopPropagation()}><div className="modal-header"><div><span className="eyebrow">AT A GLANCE</span><h2>Your shift-side companion.</h2></div><button className="icon-button" onClick={() => setHelp(false)} aria-label="Close guide"><X /></button></div><div className="help-steps"><p><strong>01 · Individual change</strong>Tap the outgoing player, then a suggested replacement. Both shifts use the first tap’s game time. Show full bench opens every eligible replacement. Add player brings a player on without an outgoing player.</p><p><strong>02 · Line or pair change</strong>Open Lines & pairs for a saved line or pair. Forwards, Defence and All skaters select a group for suggested changes.</p><p><strong>03 · Mixed changes</strong>Enable Group select, select outgoing and incoming players, then tap Confirm change.</p><p><strong>04 · Independent movements</strong>Tap a player to reveal Ice, Bench and Penalty box controls. Pull or restore a goalkeeper using the team control.</p><p><strong>05 · Corrections</strong>Undo the latest change, or select an activity entry to correct its player IDs. Escape cancels a selection. Ctrl/⌘ Z undoes.</p><p><strong>Game time is authoritative.</strong>TOI stops when the clock stops. Penalty-box location does not automatically classify special teams. Player changes are disabled when disconnected.</p></div></section></div>}
    {newGame && <div className="modal-backdrop"><form className="modal" role="dialog" aria-modal="true" aria-label="Create match" onSubmit={async event => { event.preventDefault(); setCreating(true); try { await game.createGame(newName, homeName, awayName); setNewGame(false); setView('setup') } catch (e) { game.setError((e as Error).message) } finally { setCreating(false) } }}><div className="modal-header"><div><span className="eyebrow">A FRESH SHEET</span><h2>Create a match</h2></div><button type="button" className="icon-button" onClick={() => setNewGame(false)} aria-label="Close"><X /></button></div><label>Match name<input value={newName} onChange={event => setNewName(event.target.value)} placeholder="Friday night hockey" /></label><label>Home team<input required value={homeName} onChange={event => setHomeName(event.target.value)} /></label><label>Away team<input required value={awayName} onChange={event => setAwayName(event.target.value)} /></label><p className="muted">A new match starts with an independent demonstration roster. Configure or import the game roster in Match setup.</p><button disabled={creating} className="primary-button">{creating ? <LoaderCircle className="spin" size={17} /> : <Plus size={17} />}Create match</button></form></div>}
  </div>
}

const tileRole = (player: Player) => player.position === 'G' ? 'GK' : player.assigned_role || player.position

function PlayerTile({ player, selected, incoming, suggested, focused, locked, onClick }: { player: Player; selected?: boolean; incoming?: boolean; suggested?: boolean; focused?: boolean; locked: boolean; onClick: () => void }) {
  const role = tileRole(player)
  return <button data-testid={`player-${player.id}`} className={`player-tile ${player.location.toLowerCase()} ${selected ? 'outgoing-selected' : ''} ${incoming ? 'incoming-selected' : ''} ${suggested ? 'suggested-player' : ''} ${focused ? 'focused-player' : ''} ${!player.eligible ? 'unavailable' : ''}`} onClick={onClick} disabled={locked || !player.eligible} title={`#${player.number} ${player.name} · ${role}${!player.eligible ? ' · Unavailable' : ''}`} aria-label={`Number ${player.number}, ${player.name}, ${role}, ${player.location.replaceAll('_', ' ').toLowerCase()}${selected ? ', selected outgoing' : ''}${incoming ? ', selected incoming' : ''}${suggested ? ', suggested replacement' : ''}`} aria-pressed={selected || incoming || focused || false}>
    <span className="tile-position">{role}</span><strong className="jersey">{player.number}</strong>{suggested && !selected && !incoming && <span className="tile-spark" aria-hidden="true"><Sparkles size={13} /></span>}{(selected || incoming) && <span className="tile-check" aria-hidden="true"><Check size={14} strokeWidth={3} /></span>}{!player.eligible && <span className="tile-unavailable" aria-hidden="true">×</span>}
  </button>
}

type BenchGroup = { id: string; label: string; players: Player[]; line?: Line }
function groupedBench(bench: Player[], lines: Line[]): BenchGroup[] {
  const assigned = new Set<string>()
  const groups: BenchGroup[] = []
  for (const line of lines.filter(line => line.kind !== 'SPECIAL')) {
    const members = line.player_ids.map(id => bench.find(player => player.id === id && player.position !== 'G' && !assigned.has(id))).filter((player): player is Player => !!player)
    if (!members.length) continue
    members.forEach(player => assigned.add(player.id))
    groups.push({ id: line.id, label: `${line.kind === 'DEFENCE' ? 'D' : 'F'}${lines.filter(candidate => candidate.kind === line.kind).findIndex(candidate => candidate.id === line.id) + 1}`, players: members, line })
  }
  for (const [id, label, matches] of [['forwards', 'F', (player: Player) => !['D', 'G'].includes(player.position)], ['defence', 'D', (player: Player) => player.position === 'D'], ['goalkeepers', 'GK', (player: Player) => player.position === 'G']] as const) {
    const members = bench.filter(player => !assigned.has(player.id) && matches(player))
    if (members.length) groups.push({ id, label, players: members })
  }
  return groups
}

function TeamPanel({ team, state, selection, focused, suggestions, suggestionError, operationRevision, locked, onPlayer, onGroup, onLine, onMove, onAdd, onCancel, onConfirm, onSuggestionGroup }: { team: Team; state: Snapshot; selection: Selection | null; focused: string | null; suggestions: Suggestions | null; suggestionError: boolean; operationRevision: number; locked: boolean; onPlayer: (player: Player) => void; onGroup: (kind: 'FORWARD' | 'DEFENCE' | 'ALL') => void; onLine: (line: Line) => void; onMove: (id: string, to: Location) => void; onAdd: () => void; onCancel: () => void; onConfirm: () => void; onSuggestionGroup: (ids: string[]) => void }) {
  // A snapshot refresh is not a completed movement. Keep disclosures open until
  // the operator changes context or the shared action reports success.
  const contextKey = JSON.stringify([state.game.id, team.id, selection?.outgoing ?? null, focused, operationRevision])
  const [expandedBenchKey, setExpandedBenchKey] = useState<string | null>(null)
  const [expandedLinesKey, setExpandedLinesKey] = useState<string | null>(null)
  useEffect(() => { setExpandedBenchKey(null); setExpandedLinesKey(null) }, [contextKey])
  const adding = selection !== null && selection.outgoing.length === 0
  const benchOpen = adding || expandedBenchKey === contextKey
  const linesOpen = expandedLinesKey === contextKey
  const players = state.players.filter(player => player.team_id === team.id)
  const ice = orderPlayers(players.filter(player => player.location === 'ON_ICE'))
  const bench = orderPlayers(players.filter(player => player.location === 'BENCH' && player.eligible))
  const penalties = players.filter(player => player.location === 'PENALTY_BOX')
  const goalies = players.filter(player => player.position === 'G' && player.eligible)
  const goalie = goalies.find(player => player.location === 'ON_ICE')
  const reserveGoalie = goalies.find(player => player.location === 'BENCH')
  const focusedPlayer = players.find(player => player.id === focused)
  const lines = state.lines.filter(line => line.team_id === team.id)
  const benchGroups = groupedBench(bench, lines)
  const specialUnits = lines.filter(line => line.kind === 'SPECIAL')
  const initials = team.short_name || team.name.split(' ').map(part => part[0]).join('').slice(0, 3)
  const rankedIds = (suggestions?.players || []).filter(suggestion => suggestion.eligible && players.some(player => player.id === suggestion.player_id && player.location === 'BENCH' && player.eligible)).slice(0, 3).map(suggestion => suggestion.player_id)
  const groups = suggestions?.groups?.filter(group => group.player_ids.every(id => players.some(player => player.id === id && player.location === 'BENCH' && player.eligible))).slice(0, 2) || []
  const suggestedIds = selection && selection.outgoing.length > 1 && groups.length ? groups[0].player_ids : rankedIds
  const skaters = ice.filter(player => player.position !== 'G').length
  const selectPreset = (line: Line) => onLine(line)
  const selectKind = (kind: 'FORWARD' | 'DEFENCE' | 'ALL') => { setExpandedLinesKey(null); onGroup(kind) }
  return <section data-testid={`team-${team.id}`} className={`team-panel ${team.side}`} style={{ '--team-color': '#D8B95A' } as React.CSSProperties} aria-label={`${team.name} team controls`}>
    <div className="team-heading"><div className="team-identity"><span className="team-side">{team.side === 'home' ? 'HOME' : 'AWAY'}</span><h2 title={team.name}><abbr title={team.name}>{initials}</abbr><span>{team.name}</span></h2></div>{!goalie ? <span className="lineup-warning-inline"><ShieldAlert size={14} />Empty net</span> : skaters !== 5 ? <span className="lineup-warning-inline"><Users size={14} />{skaters} skaters</span> : null}</div>
    <div className="ice-section"><div className="section-label"><span>ON ICE</span></div><div className="ice-players" style={{ '--ice-columns': Math.min(7, Math.max(1, ice.length)), '--portrait-ice-columns': Math.min(3, Math.max(1, ice.length)) } as React.CSSProperties}>{ice.map(player => <PlayerTile key={player.id} player={player} selected={selection?.outgoing.includes(player.id)} locked={locked} onClick={() => onPlayer(player)} />)}{!ice.length && <div className="empty-ice">No players on ice</div>}</div></div>
    <div className="quick-select"><button aria-expanded={linesOpen} aria-controls={`line-menu-${team.id}`} onClick={() => setExpandedLinesKey(linesOpen ? null : contextKey)}>Lines & pairs<ChevronDown size={13} /></button><button disabled={locked || !bench.length} aria-expanded={adding} aria-controls={`bench-${team.id}`} onClick={adding ? onCancel : onAdd}><Plus size={15} />Add player</button>
      {linesOpen && <div className="line-menu" id={`line-menu-${team.id}`}><div className="line-menu-kinds"><button disabled={locked || !ice.some(player => !['D', 'G'].includes(player.position))} onClick={() => selectKind('FORWARD')}>Forwards</button><button disabled={locked || !ice.some(player => player.position === 'D')} onClick={() => selectKind('DEFENCE')}>Defence</button><button disabled={locked || !skaters} onClick={() => selectKind('ALL')}>All skaters</button></div>{lines.filter(line => line.kind !== 'SPECIAL').map(line => {
        const members = players.filter(player => line.player_ids.includes(player.id))
        const available = members.length > 0 && members.some(player => player.location === 'BENCH') && members.every(player => player.eligible && player.location !== 'PENALTY_BOX')
        return <button key={line.id} className="line-menu-preset" aria-label={`Change ${line.name}`} disabled={locked || !available} onClick={() => selectPreset(line)}><strong>{line.name}</strong><span>{members.map(player => `#${player.number}`).join(' · ')}</span><ArrowRight size={14} /></button>
      })}<button className="goalie-button" disabled={locked || (!goalie && !reserveGoalie)} onClick={() => { if (goalie) onMove(goalie.id, 'BENCH'); else if (reserveGoalie) onMove(reserveGoalie.id, 'ON_ICE') }}><ArrowLeftRight size={14} />{goalie ? 'Pull goalie' : 'Restore goalie'}</button></div>}
    </div>
    {penalties.length > 0 && <div className={`penalty-area ${penalties.length ? 'occupied' : ''}`}><div className="penalty-label"><ShieldAlert size={15} /><strong>PENALTY BOX</strong></div><div className="penalty-players">{penalties.length ? penalties.map(player => <button key={player.id} disabled={locked} className={focused === player.id ? 'selected' : ''} onClick={() => onPlayer(player)} aria-label={`Number ${player.number}, ${player.name}, penalty box`} aria-pressed={focused === player.id} title={`#${player.number} ${player.name}`}><strong>{player.number}</strong>{focused === player.id && <Check size={14} />}</button>) : <span className="penalty-empty">—</span>}</div></div>}
    {(selection || focusedPlayer) && <div className="selection-context"><div className="selection-summary"><strong>{adding ? 'Add player' : selection ? selection.outgoing.map(id => `#${players.find(player => player.id === id)?.number}`).join(' + ') : `#${focusedPlayer?.number}`}</strong>{selection && !adding && <ArrowRight size={14} />}{selection?.incoming.length ? <strong>{selection.incoming.map(id => `#${players.find(player => player.id === id)?.number}`).join(' + ')}</strong> : null}{selection && <button onClick={onCancel} className="context-cancel" aria-label="Cancel selection"><X size={17} />Cancel</button>}</div>
      {selection && !adding && <button className="context-bench-toggle" aria-expanded={benchOpen} aria-controls={`bench-${team.id}`} onClick={() => setExpandedBenchKey(benchOpen ? null : contextKey)}>{benchOpen ? 'Hide full bench' : 'Show full bench'}<ChevronDown size={13} /></button>}
      {selection && selection.outgoing.length > 1 && !selection.incoming.length && groups.length > 0 && <div className="group-suggestions" aria-label="Suggested groups">{groups.map((group, index) => <button disabled={locked} key={index} aria-label={`Suggested group ${group.player_ids.map(id => players.find(player => player.id === id)?.number).join(', ')}`} onClick={() => onSuggestionGroup(group.player_ids)}><Sparkles size={14} />{group.player_ids.map(id => <strong key={id}>{players.find(player => player.id === id)?.number}</strong>)}<ArrowRight size={14} /></button>)}</div>}
      {selection && selection.outgoing.length > 1 && specialUnits.length > 0 && <details className="special-units"><summary>Special units<ChevronDown size={14} /></summary><div className="special-unit-menu">{specialUnits.map(line => {
        const members = players.filter(player => line.player_ids.includes(player.id))
        const incoming = members.filter(player => player.location === 'BENCH')
        const unavailable = !members.length || !incoming.length || members.some(player => !player.eligible || player.location === 'PENALTY_BOX')
        return <button key={line.id} aria-label={`Change special unit ${line.name}`} disabled={locked || unavailable} onClick={() => selectPreset(line)}><strong>{line.name}</strong><span>{members.map(player => `#${player.number}`).join(' · ')}</span><ArrowRight size={14} /></button>
      })}</div></details>}
      {!!selection?.incoming.length && <button className="primary-button confirm-change" disabled={locked} onClick={onConfirm}><Check size={16} />Confirm change</button>}
      {focusedPlayer && (!selection || selection.outgoing.length === 1) && <div className="movement-controls">{(['ON_ICE', 'BENCH', 'PENALTY_BOX'] as Location[]).filter(to => to !== focusedPlayer.location).map(to => <button disabled={locked} key={to} onClick={() => onMove(focusedPlayer.id, to)}>{to === 'ON_ICE' ? <Play size={13} /> : to === 'PENALTY_BOX' ? <Square size={13} /> : <ArrowDownToLine size={14} />}{focusedPlayer.location === 'PENALTY_BOX' ? (to === 'ON_ICE' ? 'Return to ice' : 'Return to bench') : to === 'ON_ICE' ? 'Add to ice' : to === 'BENCH' ? 'Move to bench' : 'Send to penalty box'}</button>)}{!selection && <button className="icon-button" onClick={onCancel} aria-label="Close movement controls"><X size={16} /></button>}</div>}
      {suggestionError && <span className="suggestion-failure" role="status">Suggestions unavailable</span>}
    </div>}
    {selection?.outgoing.length === 1 && !benchOpen && <div className="immediate-replacements" aria-label="Suggested replacements">{rankedIds.map(id => players.find(player => player.id === id)!).map(player => <PlayerTile key={player.id} player={player} incoming={selection.incoming.includes(player.id)} suggested locked={locked} onClick={() => onPlayer(player)} />)}{!suggestions && !suggestionError && <span className="replacement-loading" role="status"><LoaderCircle className="spin" size={15} />Finding replacements…</span>}{suggestions && !rankedIds.length && <span className="replacement-loading">No suggestions</span>}</div>}
    {benchOpen && <div className="bench-section" id={`bench-${team.id}`} aria-label={adding ? 'Available players to add' : 'Full bench'}><div className="bench-groups">{benchGroups.map(group => <div className="bench-group" key={group.id}><span className="bench-group-label" title={group.line?.name}>{group.label}</span><div className="bench-players">{group.players.map(player => <PlayerTile key={player.id} player={player} incoming={selection?.incoming.includes(player.id)} suggested={suggestedIds.includes(player.id)} focused={!selection && focused === player.id} locked={locked} onClick={() => onPlayer(player)} />)}</div></div>)}</div></div>}
  </section>
}

export function eventDescription(event: MatchEvent, state: Snapshot) {
  const numbers = (value: unknown) => (Array.isArray(value) ? value : []).map(id => `#${state.players.find(player => player.id === id)?.number ?? '?'}`).join(', ')
  if (event.type === 'substitute') return `${numbers(event.payload.outgoing_ids)} → ${numbers(event.payload.incoming_ids)}`
  if (event.type === 'move') return `#${state.players.find(player => player.id === event.payload.player_id)?.number ?? '?'} → ${String(event.payload.to || '').replaceAll('_', ' ').toLowerCase()}`
  if (event.type === 'clock') return `Clock ${event.payload.operation || 'updated'}`
  if (event.type === 'setup') return 'Match configuration updated'
  if (event.type === 'correct') return 'Historical movement corrected'
  if (event.type === 'undo') return 'Movement undone'
  return event.type.replaceAll('_', ' ')
}

function RecentActivity({ state, locked, onCorrect, onUndo }: { state: Snapshot; locked: boolean; onCorrect: (event: MatchEvent) => void; onUndo: (id: string) => void }) {
  const [expanded, setExpanded] = useState(false)
  const events = [...state.events].reverse().slice(0, expanded ? 30 : 5)
  return <section className="activity-panel"><div className="activity-heading"><div><History size={17} /><h2>Recent activity</h2><span className="activity-count">{state.events.length}</span></div><button className="text-button" onClick={() => setExpanded(!expanded)}>{expanded ? 'Show recent' : 'View timeline'}<ArrowRight size={14} /></button></div><div className="activity-list">{events.length ? events.map(event => {
    const playerId = event.payload.player_id || (event.payload.outgoing_ids as string[] | undefined)?.[0]
    const team = state.teams.find(team => team.id === (event.payload.team_id || state.players.find(player => player.id === playerId)?.team_id))
    const editable = ['move', 'substitute'].includes(event.type) && !event.voided
    return <div key={event.id} className={`activity-event ${event.voided ? 'voided' : ''}`}><span className={`event-icon ${event.type === 'move' ? 'movement' : ''}`}>{event.type === 'clock' ? <Clock3 size={15} /> : event.type === 'substitute' ? <ArrowLeftRight size={15} /> : <Activity size={15} />}</span><span className="event-time">P{event.period} <strong>{duration(event.remaining_ms)}</strong></span><span className="event-team">{team?.short_name || 'MATCH'}</span><span className="event-description">{eventDescription(event, state)}{event.voided && <small>UNDONE</small>}</span>{editable && <div className="event-actions"><button disabled={locked} onClick={() => onCorrect(event)}>Correct</button><button disabled={locked} onClick={() => onUndo(event.id)} aria-label={`Undo ${eventDescription(event, state)}`}><RotateCcw size={14} /></button></div>}</div>
  }) : <div className="empty-activity"><span className="status-dot" />The match is ready. Your first change will appear here.</div>}</div></section>
}

function Statistics({ state, elapsed, onExport }: { state: Snapshot; elapsed: number; onExport: () => void }) {
  const [teamId, setTeamId] = useState('all')
  const [playerId, setPlayerId] = useState<string | null>(null)
  const [sort, setSort] = useState<'toi' | 'number' | 'shifts'>('toi')
  const players = state.players.filter(player => teamId === 'all' || player.team_id === teamId).sort((a, b) => sort === 'toi' ? b.total_toi_ms - a.total_toi_ms : sort === 'number' ? Number(a.number) - Number(b.number) : b.completed_shifts - a.completed_shifts)
  const selected = state.players.find(player => player.id === playerId)
  const active = state.players.filter(player => player.location === 'ON_ICE' && player.eligible)
  return <section className="statistics"><div className="statistics-summary"><div><span className="eyebrow">PLAYING TIME</span><strong>{duration(state.clock.playing_ms + elapsed)}</strong><small>Across all periods</small></div><div><span className="eyebrow">ACTIVE PLAYERS</span><strong>{active.length}</strong><small>{state.teams.map(team => `${team.short_name} ${active.filter(player => player.team_id === team.id).length}`).join(' · ')}</small></div><div><span className="eyebrow">COMPLETED SHIFTS</span><strong>{state.players.reduce((sum, player) => sum + player.completed_shifts, 0)}</strong><small>Open shifts excluded</small></div><div><span className="eyebrow">TRACKED MOVEMENTS</span><strong>{state.events.filter(event => !event.voided && ['move', 'substitute'].includes(event.type)).length}</strong><small>Corrections reflected</small></div></div><div className="stats-heading"><div className="segmented"><button className={teamId === 'all' ? 'active' : ''} onClick={() => setTeamId('all')}>Both teams</button>{state.teams.map(team => <button key={team.id} className={teamId === team.id ? 'active' : ''} onClick={() => setTeamId(team.id)}>{team.short_name}</button>)}</div><div><label className="sort-select">Sort by<select value={sort} onChange={event => setSort(event.target.value as typeof sort)}><option value="toi">Time on ice</option><option value="number">Jersey number</option><option value="shifts">Completed shifts</option></select></label><button className="subtle-button" onClick={onExport}><Download size={16} />Export match JSON</button></div></div><div className="table-scroll"><table className="stats-table"><thead><tr><th>PLAYER</th><th>TEAM</th><th>LOCATION</th><th>TOTAL TOI</th><th>CURRENT</th><th>SHIFTS</th><th>AVG SHIFT</th><th>LONGEST</th>{state.game.period_lengths_ms.map((_, index) => <th key={index}>P{index + 1}</th>)}</tr></thead><tbody>{players.map(player => <tr key={player.id} className={playerId === player.id ? 'selected' : ''} onClick={() => setPlayerId(playerId === player.id ? null : player.id)}><td><button className="stats-player" onClick={event => { event.stopPropagation(); setPlayerId(playerId === player.id ? null : player.id) }}><strong>{player.number}</strong><span>{player.name}<small>{player.assigned_role || player.position}{!player.eligible && ' · Unavailable'}</small></span><ChevronDown size={13} /></button></td><td>{state.teams.find(team => team.id === player.team_id)?.short_name}</td><td><span className={`location-badge ${player.location.toLowerCase()}`}>{player.location === 'ON_ICE' ? '● On ice' : player.location === 'BENCH' ? 'Bench' : 'Penalty box'}</span></td><td className="toi-cell">{duration(player.total_toi_ms + (player.location === 'ON_ICE' && player.eligible ? elapsed : 0))}</td><td>{player.location === 'ON_ICE' && player.eligible ? duration(player.current_shift_ms + elapsed) : '—'}</td><td>{player.completed_shifts}</td><td>{duration(player.average_shift_ms)}</td><td>{duration(player.longest_shift_ms)}</td>{state.game.period_lengths_ms.map((_, index) => <td key={index}>{duration((player.toi_by_period?.[String(index + 1)] || 0) + (player.location === 'ON_ICE' && player.eligible && state.clock.period === index + 1 ? elapsed : 0))}</td>)}</tr>)}</tbody></table></div>{selected && <div className="shift-history"><h3><History size={16} />#{selected.number} {selected.name} · Shift history</h3><div>{state.shifts.filter(shift => shift.player_id === selected.id).map((shift, index) => {
    const start = Number(shift.start_ms ?? shift.start_playing_ms ?? 0)
    const endValue = shift.end_ms ?? shift.end_playing_ms
    const end = endValue === null || endValue === undefined ? state.clock.playing_ms + elapsed : Number(endValue)
    return <span key={String(shift.id || index)}><strong>Shift {index + 1}</strong>P{String(shift.period ?? shift.start_period ?? '—')} · {duration(end - start)}<small>{endValue === null || endValue === undefined ? 'ACTIVE' : 'COMPLETED'}</small></span>
  })}</div></div>}<p className="stats-note"><CircleHelp size={14} />Special-teams TOI is unavailable until an authoritative playing-strength timeline is connected. Player locations alone do not classify penalties.</p></section>
}
