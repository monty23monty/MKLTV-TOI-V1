import { useCallback, useEffect, useRef, useState } from 'react'
import type { GameSummary, Player, Snapshot } from './types'
import { createUuid } from './uuid'

const base = import.meta.env?.VITE_API_BASE_URL || ''

export async function api<T>(path: string, options?: RequestInit, timeoutMs = 10000): Promise<T> {
  const token = sessionStorage.getItem('toi-operator-token') || ''
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(`${base}${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}), ...options?.headers }, signal: controller.signal })
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      const message = typeof body.detail === 'string' ? body.detail : body.detail?.message || JSON.stringify(body.detail || body.error || 'Request failed')
      throw Object.assign(new Error(message), { status: response.status })
    }
    return await response.json() as T
  } finally { window.clearTimeout(timeout) }
}

export function useGame() {
  const [games, setGames] = useState<GameSummary[]>([])
  const [gameId, setGameId] = useState<string>(() => localStorage.getItem('lightning-toi-game') || '')
  const [state, setState] = useState<Snapshot | null>(null)
  const [connection, setConnection] = useState<'connecting' | 'live' | 'offline'>('connecting')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [now, setNow] = useState(performance.now())
  const stateRef = useRef<Snapshot | null>(null)
  const receivedAt = useRef(performance.now())
  const pendingRef = useRef(false)
  const gameIdRef = useRef(gameId)

  const adopt = useCallback((snapshot: Snapshot) => {
    if (snapshot.game.id !== gameIdRef.current) return
    if (stateRef.current && (snapshot.version < stateRef.current.version || (snapshot.version === stateRef.current.version && (snapshot.server_time_ms ?? 0) < (stateRef.current.server_time_ms ?? 0)))) return
    receivedAt.current = performance.now()
    stateRef.current = snapshot
    setState(snapshot)
    setNow(performance.now())
  }, [])

  const refreshGames = useCallback(async () => {
    const list = await api<GameSummary[]>('/api/v1/games')
    setGames(list)
    return list
  }, [])

  const selectGame = useCallback((id: string) => {
    gameIdRef.current = id
    stateRef.current = null
    setState(null)
    setGameId(id)
    setConnection('connecting')
    localStorage.setItem('lightning-toi-game', id)
  }, [])

  useEffect(() => {
    let active = true
    refreshGames().then(list => {
      if (!active) return
      if (!list.some(game => game.id === gameIdRef.current) && list[0]) selectGame(list[0].id)
    }).catch(e => active && setError(`Cannot load matches: ${e.message}`))
    return () => { active = false }
  }, [refreshGames, selectGame])

  useEffect(() => {
    const timer = window.setInterval(() => setNow(performance.now()), 200)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!gameId) return
    let disposed = false
    let socket: WebSocket | null = null
    let timer = 0
    let attempt = 0
    const sync = async () => {
      try { const snapshot = await api<Snapshot>(`/api/v1/games/${gameId}/state`); if (!disposed) adopt(snapshot) }
      catch (e) { if (!disposed) setError(`State synchronisation failed: ${(e as Error).message}`) }
    }
    const connect = () => {
      const host = base ? new URL(base, location.href) : new URL(location.href)
      const protocol = host.protocol === 'https:' ? 'wss:' : 'ws:'
      const token = sessionStorage.getItem('toi-operator-token') || ''
      socket = new WebSocket(`${protocol}//${host.host}/api/v1/games/${gameId}/ws${token ? `?token=${encodeURIComponent(token)}` : ''}`)
      socket.onopen = () => { if (disposed) return; attempt = 0; setConnection('live'); void sync() }
      socket.onmessage = event => {
        try {
          const message = JSON.parse(event.data)
          if (message.state) adopt(message.state)
          else if (message.type === 'clock.tick' && stateRef.current) {
            const current = stateRef.current
            if (message.version > current.version) { void sync(); return }
            if (message.version < current.version) return
            const toi = new Map<string, Partial<Player>>(message.toi?.map((entry: { player_id: string }) => [entry.player_id, entry]) || [])
            adopt({ ...current, clock: message.clock, players: current.players.map(player => ({ ...player, ...toi.get(player.id) })), warnings: message.warnings ?? current.warnings, strengths: message.strengths ?? current.strengths, server_time_ms: message.server_time_ms })
          }
        }
        catch { setError('Received an unreadable live update. Resynchronising.'); void sync() }
      }
      socket.onerror = () => socket?.close()
      socket.onclose = () => {
        if (disposed) return
        setConnection('offline')
        timer = window.setTimeout(connect, Math.min(1000 * 2 ** attempt++, 10000))
      }
    }
    void sync()
    connect()
    return () => { disposed = true; window.clearTimeout(timer); socket?.close() }
  }, [gameId, adopt])

  const action = useCallback(async (type: string, payload: Record<string, unknown>): Promise<boolean> => {
    if (pendingRef.current || !stateRef.current || connection !== 'live') return false
    pendingRef.current = true
    setPending(true)
    setError('')
    const id = gameIdRef.current
    try {
      const body = JSON.stringify({ request_id: createUuid(), expected_version: stateRef.current.version, type, payload })
      let snapshot: Snapshot
      try { snapshot = await api<Snapshot>(`/api/v1/games/${id}/actions`, { method: 'POST', body }) }
      catch (e) {
        if ((e as { status?: number }).status) throw e
        // Retrying the identical request ID makes a lost response safe.
        snapshot = await api<Snapshot>(`/api/v1/games/${id}/actions`, { method: 'POST', body })
      }
      adopt(snapshot)
      return true
    } catch (e) {
      if ((e as { status?: number }).status === 409) {
        setError('Another operator changed this match. The latest state has been loaded; please select the change again.')
        try { adopt(await api<Snapshot>(`/api/v1/games/${id}/state`)) } catch { setError('Could not resynchronise. Waiting for the live connection to recover.') }
      } else setError((e as Error).name === 'AbortError' ? 'Server did not respond. Your change may have been saved; resynchronising before the next action.' : (e as Error).message)
      try { adopt(await api<Snapshot>(`/api/v1/games/${id}/state`)) } catch { /* The visible action error remains until connection recovers. */ }
      return false
    } finally { pendingRef.current = false; setPending(false) }
  }, [adopt, connection])

  const createGame = async (name: string, home: string, away: string) => {
    setError('')
    const snapshot = await api<Snapshot>('/api/v1/games', { method: 'POST', body: JSON.stringify({ name, home_name: home, away_name: away }) })
    await refreshGames()
    selectGame(snapshot.game.id)
  }

  const acceptImported = async (snapshot: Snapshot) => {
    await refreshGames()
    selectGame(snapshot.game.id)
    adopt(snapshot)
  }

  const resync = async () => {
    const snapshot = await api<Snapshot>(`/api/v1/games/${gameIdRef.current}/state`)
    adopt(snapshot)
    await refreshGames()
    return snapshot
  }

  const elapsed = state?.clock.running ? Math.max(0, Math.min(now - receivedAt.current, state.clock.remaining_ms)) : 0
  return { state, games, gameId, connection, pending, error, setError, action, createGame, selectGame, elapsed, refreshGames, acceptImported, resync, applySnapshot: adopt }
}
