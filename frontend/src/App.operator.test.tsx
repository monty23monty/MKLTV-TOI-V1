import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'
import App from './App'
import type { Action, Line, Player, Snapshot, Suggestions } from './types'
import { api, useGame } from './useGame'

vi.mock('./useGame', () => ({ api: vi.fn(), useGame: vi.fn() }))

type GameHook = ReturnType<typeof useGame>
const mockedUseGame = vi.mocked(useGame)
const mockedApi = vi.mocked(api)
let sendAction: Mock<Action>
let fullscreenElement: Element | null
let requestFullscreen: Mock<(this: HTMLElement) => Promise<void>>
let exitFullscreen: Mock<() => Promise<void>>
let restoreProperties: (() => void)[] = []

/** An isolated imported-roster-shaped snapshot; these tests never contact a server. */
function fixture(): Snapshot {
  const players: Player[] = []
  const lines: Line[] = []
  for (const teamId of ['home', 'away']) {
    const positions: Player['position'][] = ['LW', 'C', 'RW', 'LW', 'C', 'RW', 'D', 'D', 'D', 'D', 'G', 'G']
    const roles = ['LW', 'C', 'RW', 'LW', 'C', 'RW', 'LD', 'RD', 'LD', 'RD', 'GK', 'GK']
    positions.forEach((position, index) => {
      const number = index + 1
      players.push({
        id: `${teamId}-${number}`, team_id: teamId, name: `${teamId} Fixture Player ${number}`,
        number: String(number), position, assigned_role: roles[index], eligible: true,
        location: [1, 2, 3, 7, 8, 11].includes(number) ? 'ON_ICE' : 'BENCH',
        total_toi_ms: 68123, current_shift_ms: 12345, completed_shifts: 3,
        average_shift_ms: 18200, longest_shift_ms: 23100, toi_by_period: { '1': 68123 }, strength_toi: {},
      })
    })
    for (const [suffix, name, kind, numbers] of [
      ['f1', 'Forward line 1', 'FORWARD', [1, 2, 3]],
      ['f2', 'Forward line 2', 'FORWARD', [4, 5, 6]],
      ['d1', 'Defensive pair 1', 'DEFENCE', [7, 8]],
      ['d2', 'Defensive pair 2', 'DEFENCE', [9, 10]],
    ] as const) lines.push({ id: `${teamId}-${suffix}`, team_id: teamId, name, kind, player_ids: numbers.map(number => `${teamId}-${number}`) })
  }
  return {
    game: { id: 'operator-regression', name: 'Isolated operator fixture', status: 'LIVE', competition: 'Fixture league', period_lengths_ms: [1200000, 1200000, 1200000] },
    teams: [
      { id: 'home', name: 'Home Fixture', short_name: 'HOM', color: '#D8B95A', side: 'home' },
      { id: 'away', name: 'Away Fixture', short_name: 'AWY', color: '#D8B95A', side: 'away' },
    ],
    players, lines,
    clock: { period: 1, remaining_ms: 1187655, running: true, playing_ms: 12345, anchor_unix_ms: 100000, period_playing_ms: { '1': 12345 } },
    events: [{ id: 'prior-move', type: 'move', payload: { player_id: 'home-11', to: 'ON_ICE' }, playing_ms: 0, period: 1, remaining_ms: 1200000, actor: 'Fixture operator', voided: false }],
    shifts: [], version: 7, server_time_ms: 100000, warnings: [], strengths: {},
  }
}

const suggestions: Suggestions = {
  players: [{ player_id: 'home-4', score: 99, reasons: ['Long enough rest', 'Same forward role'], eligible: true, observations: 4 }],
  groups: [{ player_ids: ['home-4', 'home-5', 'home-6'], score: 98, reasons: ['The next forward line'], name: 'Forward line 2' }],
  combinations: [],
}

function setProperty(target: object, key: string, descriptor: PropertyDescriptor) {
  const previous = Object.getOwnPropertyDescriptor(target, key)
  Object.defineProperty(target, key, { configurable: true, ...descriptor })
  restoreProperties.push(() => { if (previous) Object.defineProperty(target, key, previous); else Reflect.deleteProperty(target, key) })
}

function start(snapshot = fixture(), overrides: Partial<GameHook> = {}) {
  const game: GameHook = {
    state: snapshot, games: [{ id: snapshot.game.id, name: snapshot.game.name, status: snapshot.game.status }],
    gameId: snapshot.game.id, connection: 'live', pending: false, error: '', elapsed: 432.75,
    setError: vi.fn(), action: sendAction, createGame: vi.fn().mockResolvedValue(undefined),
    selectGame: vi.fn(), refreshGames: vi.fn().mockResolvedValue([]), acceptImported: vi.fn().mockResolvedValue(undefined),
    resync: vi.fn().mockResolvedValue(snapshot), applySnapshot: vi.fn(), ...overrides,
  }
  mockedUseGame.mockImplementation(() => game)
  const rendered = render(<App />)
  return { game, ...rendered }
}

const tile = (id: string) => screen.getByTestId(`player-${id}`)
const homeControls = () => screen.getByRole('region', { name: 'Home Fixture team controls' })

function openTeamMenu() {
  const trigger = within(homeControls()).getByRole('button', { name: 'Lines & pairs' })
  if (trigger.getAttribute('aria-expanded') !== 'true') fireEvent.click(trigger)
}
function selectTeamGroup(name: 'Forwards' | 'Defence' | 'All skaters') {
  openTeamMenu()
  fireEvent.click(within(homeControls()).getByRole('button', { name }))
}
function changePreset(name: string) {
  openTeamMenu()
  fireEvent.click(within(homeControls()).getByRole('button', { name: `Change ${name}` }))
}
function openBench() {
  const trigger = within(homeControls()).getByRole('button', { name: /^(Bench|All replacements)$/ })
  if (trigger.getAttribute('aria-expanded') !== 'true') fireEvent.click(trigger)
}

async function enterOperatorMode() {
  fireEvent.click(screen.getByTestId('operator-enter'))
  await waitFor(() => expect(screen.getByTestId('operator-root')).toHaveClass('operator-mode'))
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  mockedUseGame.mockReset()
  mockedApi.mockReset()
  mockedApi.mockResolvedValue(suggestions)
  sendAction = vi.fn<Action>().mockResolvedValue(true)
  fullscreenElement = null
  requestFullscreen = vi.fn(async function (this: HTMLElement) {
    fullscreenElement = this
    document.dispatchEvent(new Event('fullscreenchange'))
  })
  exitFullscreen = vi.fn(async () => {
    fullscreenElement = null
    document.dispatchEvent(new Event('fullscreenchange'))
  })
  setProperty(document, 'fullscreenElement', { get: () => fullscreenElement })
  setProperty(HTMLElement.prototype, 'requestFullscreen', { value: requestFullscreen })
  setProperty(document, 'exitFullscreen', { value: exitFullscreen })
})

afterEach(() => {
  for (const restore of restoreProperties.reverse()) restore()
  restoreProperties = []
})

describe('minimal operator dashboard', () => {
  it('shows only both on-ice lineups at rest, keeping bench players, names and statistics out of the main surface', () => {
    const { container } = start()
    expect(homeControls()).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Away Fixture team controls' })).toBeInTheDocument()
    expect(container.querySelectorAll('.ice-players .player-tile')).toHaveLength(12)
    expect(container.querySelectorAll('.player-tile')).toHaveLength(12)
    expect(screen.queryByTestId('player-home-4')).not.toBeInTheDocument()
    expect(within(homeControls()).getByRole('button', { name: 'Bench' })).toHaveAttribute('aria-expanded', 'false')
    expect(tile('home-7')).toHaveTextContent('LD')
    expect(tile('home-8')).toHaveTextContent('RD')
    expect(tile('home-11')).toHaveTextContent('GK')
    for (const player of fixture().players.filter(player => player.location === 'ON_ICE')) {
      const element = tile(player.id)
      expect(element).toHaveTextContent(String(player.number))
      expect(element.textContent).not.toContain(player.name)
      expect(element.textContent).not.toMatch(/TOI|SHIFT|68:|12:|Fixture/)
    }
    expect(screen.queryByText('Recent activity')).not.toBeInTheDocument()
    expect(screen.queryByText('Recommendation scoring')).not.toBeInTheDocument()
    expect(screen.queryByText('Long enough rest')).not.toBeInTheDocument()
  })

  it('preserves selected outgoing players and the same team components across native entry and visible exit', async () => {
    const { game } = start()
    const originalState = structuredClone(game.state)
    const originalHome = homeControls()
    const originalTile = tile('home-1')
    fireEvent.click(originalTile)
    expect(originalTile).toHaveAttribute('aria-pressed', 'true')

    await enterOperatorMode()
    expect(requestFullscreen).toHaveBeenCalledOnce()
    expect(fullscreenElement).toBe(screen.getByTestId('operator-root'))
    expect(screen.queryByRole('navigation', { name: 'Main navigation' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Match setup' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /API documentation/ })).not.toBeInTheDocument()
    expect(homeControls()).toBe(originalHome)
    expect(tile('home-1')).toBe(originalTile)
    expect(originalTile).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(screen.getByTestId('operator-exit'))
    await waitFor(() => expect(screen.getByTestId('operator-root')).not.toHaveClass('operator-mode'))
    expect(exitFullscreen).toHaveBeenCalledOnce()
    expect(screen.getByRole('navigation', { name: 'Main navigation' })).toBeInTheDocument()
    expect(homeControls()).toBe(originalHome)
    expect(tile('home-1')).toBe(originalTile)
    expect(originalTile).toHaveAttribute('aria-pressed', 'true')
    expect(game.state).toEqual(originalState)
    expect(game.selectGame).not.toHaveBeenCalled()
    expect(sendAction).not.toHaveBeenCalled()
  })

  it('synchronises browser-driven fullscreen exit while retaining the current selection and tracking state', async () => {
    const { game } = start()
    fireEvent.click(tile('home-1'))
    const snapshot = game.state
    await enterOperatorMode()
    act(() => {
      fullscreenElement = null
      document.dispatchEvent(new Event('fullscreenchange'))
    })
    await waitFor(() => expect(screen.getByTestId('operator-root')).not.toHaveClass('operator-mode'))
    expect(tile('home-1')).toHaveAttribute('aria-pressed', 'true')
    expect(game.state).toBe(snapshot)
    expect(sendAction).not.toHaveBeenCalled()
  })

  it('exits on Escape without treating it as a cancellation of the current player selection', async () => {
    start()
    fireEvent.click(tile('home-1'))
    await enterOperatorMode()
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.getByTestId('operator-root')).not.toHaveClass('operator-mode'))
    expect(tile('home-1')).toHaveAttribute('aria-pressed', 'true')
    expect(sendAction).not.toHaveBeenCalled()
  })

  it('offers a usable focus layout after browser rejection, with the selected lineup intact', async () => {
    requestFullscreen.mockRejectedValue(new Error('Fullscreen is not allowed in this embed'))
    start()
    fireEvent.click(tile('home-1'))
    await enterOperatorMode()
    expect(fullscreenElement).toBeNull()
    await waitFor(() => expect(screen.getByTestId('operator-exit')).toHaveAccessibleName('Exit focus layout'))
    expect(screen.queryByRole('navigation', { name: 'Main navigation' })).not.toBeInTheDocument()
    expect(tile('home-1')).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(await screen.findByTestId('player-home-4'))
    await waitFor(() => expect(sendAction).toHaveBeenCalledWith('substitute', {
      team_id: 'home', outgoing_ids: ['home-1'], incoming_ids: ['home-4'], at_playing_ms: 12777,
    }))
    fireEvent.click(screen.getByTestId('operator-exit'))
    await waitFor(() => expect(screen.getByTestId('operator-root')).not.toHaveClass('operator-mode'))
    expect(exitFullscreen).not.toHaveBeenCalled()
  })

  it('makes a two-touch fullscreen substitution at the first tap game time and adopts the authoritative roster update', async () => {
    const user = userEvent.setup()
    const { game, rerender } = start()
    await enterOperatorMode()
    await user.pointer([{ keys: '[TouchA>]', target: tile('home-1') }, { keys: '[/TouchA]' }])
    expect(tile('home-1')).toHaveAttribute('aria-pressed', 'true')
    await waitFor(() => expect(mockedApi).toHaveBeenCalledWith('/api/v1/games/operator-regression/suggestions?team_id=home&outgoing_ids=home-1'))
    expect(sendAction).not.toHaveBeenCalled()
    game.elapsed = 3000
    rerender(<App />)
    await user.pointer([{ keys: '[TouchA>]', target: tile('home-4') }, { keys: '[/TouchA]' }])
    await waitFor(() => expect(sendAction).toHaveBeenCalledExactlyOnceWith('substitute', {
      team_id: 'home', outgoing_ids: ['home-1'], incoming_ids: ['home-4'], at_playing_ms: 12777,
    }))
    game.state = { ...game.state!, version: 8, players: game.state!.players.map(player => player.id === 'home-1' ? { ...player, location: 'BENCH' } : player.id === 'home-4' ? { ...player, location: 'ON_ICE' } : player) }
    rerender(<App />)
    expect(screen.queryByTestId('player-home-1')).not.toBeInTheDocument()
    expect(within(homeControls()).getByRole('button', { name: 'Bench' })).toHaveAttribute('aria-expanded', 'false')
    expect(tile('home-4')).toHaveClass('on_ice')
    expect(tile('home-4')).toHaveAttribute('aria-pressed', 'false')
  })

  it('shows suggested replacements on selection and reveals alternatives only when requested without duplicate tiles', async () => {
    start()
    fireEvent.click(tile('home-1'))
    await waitFor(() => expect(tile('home-4')).toHaveClass('suggested-player'))
    expect(screen.getAllByTestId('player-home-4')).toHaveLength(1)
    expect(screen.queryByTestId('player-home-5')).not.toBeInTheDocument()
    expect(screen.queryByTestId('player-home-9')).not.toBeInTheDocument()
    openBench()
    expect(screen.getAllByTestId('player-home-4')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'All replacements' })).toHaveAttribute('aria-expanded', 'true')
    expect(tile('home-5')).toBeEnabled()
    expect(tile('home-9')).toBeEnabled()
    expect(screen.queryByText('Long enough rest')).not.toBeInTheDocument()
    expect(screen.queryByText('Same forward role')).not.toBeInTheDocument()
    expect(screen.queryByText('home Fixture Player 4')).not.toBeInTheDocument()
    fireEvent.click(tile('home-5'))
    await waitFor(() => expect(sendAction).toHaveBeenCalledExactlyOnceWith('substitute', {
      team_id: 'home', outgoing_ids: ['home-1'], incoming_ids: ['home-5'], at_playing_ms: 12777,
    }))
  })

  it.each([
    ['Forward line 2', ['home-1', 'home-2', 'home-3'], ['home-4', 'home-5', 'home-6']],
    ['Defensive pair 2', ['home-7', 'home-8'], ['home-9', 'home-10']],
  ])('changes %s in one atomic fullscreen action', async (name, outgoing, incoming) => {
    start()
    await enterOperatorMode()
    changePreset(name)
    await waitFor(() => expect(sendAction).toHaveBeenCalledExactlyOnceWith('substitute', {
      team_id: 'home', outgoing_ids: outgoing, incoming_ids: incoming, at_playing_ms: 12777,
    }))
  })

  it('removes stale forward suggestions immediately when a defensive selection is awaiting new results', async () => {
    start()
    await enterOperatorMode()
    selectTeamGroup('Forwards')
    await screen.findByRole('button', { name: 'Suggested group 4, 5, 6' })
    let resolveNext: (result: Suggestions) => void = () => undefined
    mockedApi.mockImplementationOnce(() => new Promise<Suggestions>(resolve => { resolveNext = resolve }))
    selectTeamGroup('Defence')
    expect(screen.queryByRole('button', { name: 'Suggested group 4, 5, 6' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('player-home-4')).not.toBeInTheDocument()
    expect(within(homeControls()).getByRole('button', { name: 'All replacements' })).toBeEnabled()
    expect(sendAction).not.toHaveBeenCalled()
    await act(async () => resolveNext({
      players: [], combinations: [],
      groups: [{ player_ids: ['home-9', 'home-10'], score: 99, reasons: [], name: 'Defensive pair 2' }],
    }))
    fireEvent.click(await screen.findByRole('button', { name: 'Suggested group 9, 10' }))
    await waitFor(() => expect(sendAction).toHaveBeenCalledExactlyOnceWith('substitute', {
      team_id: 'home', outgoing_ids: ['home-7', 'home-8'], incoming_ids: ['home-9', 'home-10'], at_playing_ms: 12777,
    }))
  })

  it('hides suggestions from the previous snapshot until refreshed results arrive', async () => {
    const { game, rerender } = start()
    selectTeamGroup('Forwards')
    await screen.findByRole('button', { name: 'Suggested group 4, 5, 6' })
    let resolveNext: (result: Suggestions) => void = () => undefined
    mockedApi.mockImplementationOnce(() => new Promise<Suggestions>(resolve => { resolveNext = resolve }))
    game.state = { ...game.state!, version: game.state!.version + 1 }
    rerender(<App />)
    expect(screen.queryByRole('button', { name: 'Suggested group 4, 5, 6' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('player-home-4')).not.toBeInTheDocument()
    expect(sendAction).not.toHaveBeenCalled()
    await act(async () => resolveNext(suggestions))
    expect(await screen.findByRole('button', { name: 'Suggested group 4, 5, 6' })).toBeEnabled()
  })

  it('changes the complete forward line after selecting only one outgoing forward', async () => {
    const { game, rerender } = start()
    await enterOperatorMode()
    fireEvent.click(tile('home-1'))
    game.elapsed = 3000
    rerender(<App />)
    changePreset('Forward line 2')
    await waitFor(() => expect(sendAction).toHaveBeenCalledExactlyOnceWith('substitute', {
      team_id: 'home', outgoing_ids: ['home-1', 'home-2', 'home-3'], incoming_ids: ['home-4', 'home-5', 'home-6'], at_playing_ms: 12777,
    }))
  })

  it('changes only the defensive pair when its preset is tapped after selecting the forwards', async () => {
    start()
    await enterOperatorMode()
    selectTeamGroup('Forwards')
    for (const id of ['home-1', 'home-2', 'home-3']) expect(tile(id)).toHaveAttribute('aria-pressed', 'true')
    changePreset('Defensive pair 2')
    await waitFor(() => expect(sendAction).toHaveBeenCalledExactlyOnceWith('substitute', {
      team_id: 'home', outgoing_ids: ['home-7', 'home-8'], incoming_ids: ['home-9', 'home-10'], at_playing_ms: 12777,
    }))
  })

  it('offers a partially deployed saved special unit contextually and changes all remaining skaters atomically', async () => {
    const snapshot = fixture()
    snapshot.lines.push({ id: 'home-special', team_id: 'home', name: 'Power play A', kind: 'SPECIAL', player_ids: ['home-1', 'home-4', 'home-5', 'home-9', 'home-10'] })
    start(snapshot)
    await enterOperatorMode()
    expect(screen.queryByText('Special units')).not.toBeInTheDocument()
    selectTeamGroup('All skaters')
    fireEvent.click(within(homeControls()).getByText('Special units'))
    const preset = within(homeControls()).getByRole('button', { name: 'Change special unit Power play A' })
    expect(preset).toBeEnabled()
    expect(preset).toHaveTextContent('#1 · #4 · #5 · #9 · #10')
    expect(screen.getAllByTestId('player-home-1')).toHaveLength(1)
    fireEvent.click(preset)
    await waitFor(() => expect(sendAction).toHaveBeenCalledExactlyOnceWith('substitute', {
      team_id: 'home', outgoing_ids: ['home-2', 'home-3', 'home-7', 'home-8'], incoming_ids: ['home-4', 'home-5', 'home-9', 'home-10'], at_playing_ms: 12777,
    }))
  })

  it('keeps unavailable special units visible and disables every unit during pending updates', async () => {
    const snapshot = fixture()
    snapshot.players.find(player => player.id === 'home-6')!.eligible = false
    snapshot.lines.push(
      { id: 'home-special-ready', team_id: 'home', name: 'Power play A', kind: 'SPECIAL', player_ids: ['home-1', 'home-4', 'home-5', 'home-9', 'home-10'] },
      { id: 'home-special-unavailable', team_id: 'home', name: 'Unavailable unit', kind: 'SPECIAL', player_ids: ['home-1', 'home-4', 'home-6', 'home-9', 'home-10'] },
    )
    const { game, rerender } = start(snapshot)
    selectTeamGroup('Forwards')
    fireEvent.click(within(homeControls()).getByText('Special units'))
    const unavailable = within(homeControls()).getByRole('button', { name: 'Change special unit Unavailable unit' })
    expect(unavailable).toBeVisible()
    expect(unavailable).toBeDisabled()
    expect(within(homeControls()).getByRole('button', { name: 'Change special unit Power play A' })).toBeEnabled()
    game.pending = true
    rerender(<App />)
    const ready = within(homeControls()).getByRole('button', { name: 'Change special unit Power play A' })
    expect(ready).toBeDisabled()
    fireEvent.click(ready)
    fireEvent.click(unavailable)
    expect(sendAction).not.toHaveBeenCalled()
  })

  it.each([
    ['with matching counts', ['home-4', 'home-9']],
    ['with uneven counts', ['home-4']],
  ])('confirms an arbitrary mixed group %s atomically inline', async (_description, incoming) => {
    start()
    await enterOperatorMode()
    fireEvent.click(screen.getByRole('button', { name: 'Group select' }))
    fireEvent.click(tile('home-1'))
    fireEvent.click(tile('home-7'))
    openBench()
    for (const id of incoming) fireEvent.click(tile(id))
    expect(screen.getByRole('button', { name: 'Confirm change' })).toBeEnabled()
    expect(sendAction).not.toHaveBeenCalled()
    for (const id of incoming) expect(tile(id)).toHaveAttribute('aria-pressed', 'true')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm change' }))
    await waitFor(() => expect(sendAction).toHaveBeenCalledExactlyOnceWith('substitute', {
      team_id: 'home', outgoing_ids: ['home-1', 'home-7'], incoming_ids: incoming, at_playing_ms: 12777,
    }))
  })

  it.each([['To ice', 'ON_ICE'], ['To bench', 'BENCH']] as const)('returns a penalty-box player %s without leaving fullscreen', async (button, destination) => {
    const snapshot = fixture()
    snapshot.players.find(player => player.id === 'home-6')!.location = 'PENALTY_BOX'
    start(snapshot)
    await enterOperatorMode()
    fireEvent.click(within(homeControls()).getByRole('button', { name: /Number 6, home Fixture Player 6, penalty box/ }))
    fireEvent.click(within(homeControls()).getByRole('button', { name: button }))
    await waitFor(() => expect(sendAction).toHaveBeenCalledExactlyOnceWith('move', { player_id: 'home-6', to: destination }))
    expect(screen.getByTestId('operator-root')).toHaveClass('operator-mode')
  })

  it('sends an on-ice player to the penalty box and keeps immediate undo in fullscreen', async () => {
    start()
    await enterOperatorMode()
    fireEvent.click(tile('home-1'))
    fireEvent.click(within(homeControls()).getByRole('button', { name: 'To penalty box' }))
    await waitFor(() => expect(sendAction).toHaveBeenCalledWith('move', { player_id: 'home-1', to: 'PENALTY_BOX' }))
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(sendAction).toHaveBeenLastCalledWith('undo', {}))
    expect(sendAction).toHaveBeenCalledTimes(2)
  })

  it('cancels a selected outgoing player inline without moving players', () => {
    start()
    fireEvent.click(tile('home-1'))
    expect(tile('home-1')).toHaveAttribute('aria-pressed', 'true')
    openBench()
    expect(tile('home-5')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel selection' }))
    expect(tile('home-1')).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByTestId('player-home-5')).not.toBeInTheDocument()
    expect(sendAction).not.toHaveBeenCalled()
  })

  it('disables mutation controls during a pending server update so repeated taps cannot send duplicate changes', async () => {
    let finish: (value: boolean) => void = () => undefined
    sendAction.mockImplementation(() => new Promise<boolean>(resolve => { finish = resolve }))
    const { game, rerender } = start()
    await enterOperatorMode()
    fireEvent.click(tile('home-1'))
    fireEvent.click(await screen.findByTestId('player-home-4'))
    expect(sendAction).toHaveBeenCalledOnce()
    // The upstream hook owns pending state; the operator must respect it on every control.
    game.pending = true
    rerender(<App />)
    expect(tile('home-4')).toBeDisabled()
    openTeamMenu()
    expect(within(homeControls()).getByRole('button', { name: 'Change Forward line 2' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Undo' })).toBeDisabled()
    fireEvent.click(tile('home-4'))
    fireEvent.click(tile('home-4'))
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    expect(sendAction).toHaveBeenCalledOnce()
    await act(async () => finish(true))
  })

  it('prevents an unavailable or disconnected player from changing the lineup', () => {
    const snapshot = fixture()
    snapshot.players.find(player => player.id === 'home-4')!.eligible = false
    const { game, rerender } = start(snapshot)
    fireEvent.click(tile('home-1'))
    openBench()
    expect(tile('home-4')).toBeDisabled()
    fireEvent.click(tile('home-4'))
    expect(sendAction).not.toHaveBeenCalled()
    game.connection = 'offline'
    rerender(<App />)
    expect(tile('home-5')).toBeDisabled()
    fireEvent.click(tile('home-5'))
    expect(sendAction).not.toHaveBeenCalled()
  })

  it('opens the bench for an independent move and closes it after the authoritative update', async () => {
    const { game, rerender } = start()
    expect(screen.queryByTestId('player-home-4')).not.toBeInTheDocument()
    openBench()
    fireEvent.click(tile('home-4'))
    fireEvent.click(within(homeControls()).getByRole('button', { name: 'To ice' }))
    await waitFor(() => expect(sendAction).toHaveBeenCalledExactlyOnceWith('move', { player_id: 'home-4', to: 'ON_ICE' }))
    game.state = { ...game.state!, version: game.state!.version + 1, players: game.state!.players.map(player => player.id === 'home-4' ? { ...player, location: 'ON_ICE' } : player) }
    rerender(<App />)
    expect(tile('home-4')).toHaveClass('on_ice')
    expect(screen.queryByTestId('player-home-5')).not.toBeInTheDocument()
    expect(within(homeControls()).getByRole('button', { name: 'Bench' })).toHaveAttribute('aria-expanded', 'false')
  })

  it('preserves the expanded alternatives and selected player across fullscreen entry and exit', async () => {
    start()
    fireEvent.click(tile('home-1'))
    openBench()
    const originalOutgoing = tile('home-1')
    await enterOperatorMode()
    expect(tile('home-1')).toBe(originalOutgoing)
    expect(tile('home-5')).toBeInTheDocument()
    expect(within(homeControls()).getByRole('button', { name: 'All replacements' })).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(screen.getByTestId('operator-exit'))
    await waitFor(() => expect(screen.getByTestId('operator-root')).not.toHaveClass('operator-mode'))
    expect(tile('home-1')).toHaveAttribute('aria-pressed', 'true')
    expect(tile('home-5')).toBeInTheDocument()
  })

  it('retains an essential clock-feed failure warning while moving source-assignment detail to setup', async () => {
    const staleClock = 'Authoritative clock feed is stale; playing time is paused until a fresh observation arrives.'
    const sourceDetail = 'Home Fixture: Forward line 2 has empty lineup slots; the partial group is retained.'
    const snapshot = fixture()
    snapshot.warnings = [staleClock, sourceDetail]
    start(snapshot)
    expect(screen.getByText(staleClock)).toBeInTheDocument()
    expect(screen.queryByText(sourceDetail)).not.toBeInTheDocument()
    await enterOperatorMode()
    expect(screen.getByText(staleClock)).toBeInTheDocument()
    expect(screen.queryByText(sourceDetail)).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('operator-exit'))
    await waitFor(() => expect(screen.getByTestId('operator-root')).not.toHaveClass('operator-mode'))
    fireEvent.click(screen.getByRole('button', { name: 'Match setup' }))
    expect(screen.getByText(content => content.includes(sourceDetail))).toBeInTheDocument()
  })
})
