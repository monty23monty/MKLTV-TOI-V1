import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ClockSourceControl } from './panels'
import type { Action, Snapshot } from './types'

const authority = { mode: 'manual', generation: 1, source: null, service_id: null, last_source_sequence: 0, feed_status: 'manual', observed_at_ms: null, stale_at_ms: null } as const
const state = { game: { id: 'test-match' }, clock_authority: authority } as Snapshot

afterEach(() => vi.unstubAllGlobals())

describe('clock source ownership', () => {
  it('selects only configured clock sources and sends an explicit handover', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ sources: [{ source: 'scoreboard', service_id: 'clock-bridge' }] }) }))
    const action = vi.fn<Action>().mockResolvedValue(true)
    render(<ClockSourceControl state={state} action={action} locked={false} />)
    await screen.findByRole('option', { name: 'scoreboard (clock-bridge)' })
    expect(action).not.toHaveBeenCalled()
    fireEvent.change(screen.getByRole('combobox', { name: 'Use clock source' }), { target: { value: JSON.stringify(['clock-bridge', 'scoreboard']) } })
    fireEvent.click(screen.getByRole('button', { name: 'Use selected clock' }))
    await waitFor(() => expect(action).toHaveBeenCalledExactlyOnceWith('clock.authority', { mode: 'external', source: 'scoreboard', service_id: 'clock-bridge' }))
  })

  it('returns to manual ownership without a service identity', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ sources: [{ source: 'scoreboard', service_id: 'clock-bridge' }] }) }))
    const action = vi.fn<Action>().mockResolvedValue(true)
    const external = { ...state, clock_authority: { ...authority, mode: 'external' as const, source: 'scoreboard', service_id: 'clock-bridge', feed_status: 'stale' as const } }
    render(<ClockSourceControl state={external} action={action} locked={false} />)
    await screen.findByRole('option', { name: 'scoreboard (clock-bridge)' })
    expect(screen.getByRole('status')).toHaveTextContent('Waiting for clock feed')
    fireEvent.change(screen.getByRole('combobox', { name: 'Use clock source' }), { target: { value: 'manual' } })
    fireEvent.click(screen.getByRole('button', { name: 'Use selected clock' }))
    await waitFor(() => expect(action).toHaveBeenCalledExactlyOnceWith('clock.authority', { mode: 'manual', source: null, service_id: null }))
  })

  it('does not offer source changes if permission discovery is denied', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 403, json: async () => ({ detail: 'Supervisor required' }) }))
    const action = vi.fn<Action>()
    render(<ClockSourceControl state={state} action={action} locked={false} />)
    await screen.findByRole('alert')
    expect(screen.getByRole('button', { name: 'Use selected clock' })).toBeDisabled()
    expect(action).not.toHaveBeenCalled()
  })
})
