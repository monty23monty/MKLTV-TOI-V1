import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useGame } from './useGame'
import type { Snapshot } from './types'

class FakeSocket {
  static instances: FakeSocket[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  constructor(public url: string) { FakeSocket.instances.push(this) }
  close() { /* No real network is used. */ }
}

const snapshot = {
  game: { id: 'tablet-test', name: 'Tablet test', status: 'LIVE' },
  clock: { running: false, remaining_ms: 1200000 },
  players: [], version: 1, server_time_ms: 1000,
} as unknown as Snapshot

function jsonResponse(body: unknown) {
  return { ok: true, json: async () => body } as Response
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  FakeSocket.instances = []
  vi.stubGlobal('WebSocket', FakeSocket)
  vi.stubGlobal('fetch', vi.fn(async (path: string) => jsonResponse(path === '/api/v1/games' ? [snapshot.game] : snapshot)))
})
afterEach(() => vi.unstubAllGlobals())

async function connectedGame() {
  const hook = renderHook(useGame)
  await waitFor(() => expect(hook.result.current.state?.game.id).toBe(snapshot.game.id))
  act(() => FakeSocket.instances[0].onopen?.())
  await waitFor(() => expect(hook.result.current.connection).toBe('live'))
  return hook
}

describe('tablet action identifiers and pending recovery', () => {
  it('sends an action over HTTP without randomUUID and keeps the same ID on retry', async () => {
    const getRandomValues = globalThis.crypto.getRandomValues.bind(globalThis.crypto)
    vi.stubGlobal('crypto', { getRandomValues })
    const hook = await connectedGame()
    const bodies: string[] = []
    vi.mocked(fetch).mockImplementation(async (path, options) => {
      if (String(path).endsWith('/actions')) {
        bodies.push(String(options?.body))
        if (bodies.length === 1) throw new TypeError('Response lost')
      }
      return jsonResponse(snapshot)
    })
    let saved = false
    await act(async () => { saved = await hook.result.current.action('clock', { operation: 'start' }) })
    expect(saved).toBe(true)
    expect(bodies).toHaveLength(2)
    expect(bodies[0]).toBe(bodies[1])
    expect(JSON.parse(bodies[0]).request_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
    expect(hook.result.current.pending).toBe(false)
  })

  it('releases the pending guard after an identifier-generation failure', async () => {
    const randomUUID = vi.fn().mockImplementationOnce(() => { throw new Error('Random source failed') }).mockReturnValue('566493ac-d44e-4765-8f2e-d58146ac3ed5')
    vi.stubGlobal('crypto', { randomUUID })
    const hook = await connectedGame()
    let saved = true
    await act(async () => { saved = await hook.result.current.action('clock', { operation: 'start' }) })
    expect(saved).toBe(false)
    expect(hook.result.current.error).toBe('Random source failed')
    expect(hook.result.current.pending).toBe(false)
    await act(async () => { saved = await hook.result.current.action('clock', { operation: 'start' }) })
    expect(saved).toBe(true)
    expect(randomUUID).toHaveBeenCalledTimes(2)
    expect(hook.result.current.pending).toBe(false)
  })
})
