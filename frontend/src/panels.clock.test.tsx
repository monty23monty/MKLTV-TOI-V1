import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ClockEditor } from './panels'
import type { Action, Snapshot } from './types'

function clockFixture(period = 3, lengths = [1200000, 1200000, 1200000]): Snapshot {
  return {
    game: { id: 'isolated-clock', name: 'Clock fixture', status: 'LIVE', competition: '', period_lengths_ms: lengths },
    teams: [], players: [], lines: [], events: [], shifts: [], warnings: [], strengths: {}, version: 7,
    clock: { period, remaining_ms: 0, running: false, playing_ms: 3600000, anchor_unix_ms: null, period_playing_ms: { '1': 1200000, '2': 1200000, '3': 1200000 } },
  }
}

function start(state = clockFixture(), operatorMode = true, locked = false) {
  const action = vi.fn<Action>().mockResolvedValue(true)
  const onClose = vi.fn()
  render(<ClockEditor state={state} action={action} onClose={onClose} operatorMode={operatorMode} locked={locked} />)
  return { state, action, onClose }
}

describe('operator clock and overtime', () => {
  it.each([5, 20])('stages a %i-minute overtime and sends it only when Apply clock is pressed', async minutes => {
    const { state, action, onClose } = start()
    const originalState = structuredClone(state)
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Overtime minutes' }), { target: { value: String(minutes) } })
    fireEvent.click(screen.getByRole('button', { name: 'Prepare overtime' }))
    expect(screen.getByRole('combobox', { name: 'Current period' })).toHaveValue('4')
    expect(screen.getByRole('textbox', { name: 'Remaining time' })).toHaveValue(`${minutes}:00`)
    expect(screen.queryByRole('button', { name: 'Prepare overtime' })).not.toBeInTheDocument()
    expect(screen.queryByText('PERIOD LENGTHS · MINUTES')).not.toBeInTheDocument()
    expect(screen.queryByText(/Recorded playing time and shifts/)).not.toBeInTheDocument()
    expect(action).not.toHaveBeenCalled()
    expect(state).toEqual(originalState)

    fireEvent.click(screen.getByRole('button', { name: 'Apply clock' }))
    await waitFor(() => expect(action).toHaveBeenCalledExactlyOnceWith('clock', {
      operation: 'set', period: 4, remaining_ms: minutes * 60000,
      period_lengths_ms: [1200000, 1200000, 1200000, minutes * 60000],
    }))
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('can prepare another overtime after the last configured overtime without altering previous durations', async () => {
    const { action } = start(clockFixture(4, [1200000, 1200000, 1200000, 300000]))
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Overtime minutes' }), { target: { value: '20' } })
    fireEvent.click(screen.getByRole('button', { name: 'Prepare overtime' }))
    expect(screen.getByRole('combobox', { name: 'Current period' })).toHaveValue('5')
    fireEvent.click(screen.getByRole('button', { name: 'Apply clock' }))
    await waitFor(() => expect(action).toHaveBeenCalledExactlyOnceWith('clock', {
      operation: 'set', period: 5, remaining_ms: 1200000,
      period_lengths_ms: [1200000, 1200000, 1200000, 300000, 1200000],
    }))
  })

  it('keeps normal period advancement available without showing overtime before the final configured period', async () => {
    const { action, onClose } = start(clockFixture(2))
    expect(screen.queryByRole('button', { name: 'Prepare overtime' })).not.toBeInTheDocument()
    expect(screen.queryByRole('spinbutton', { name: 'Overtime minutes' })).not.toBeInTheDocument()
    expect(screen.queryByText('PERIOD LENGTHS · MINUTES')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Advance period' }))
    await waitFor(() => expect(action).toHaveBeenCalledExactlyOnceWith('clock', { operation: 'advance' }))
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('preserves the standard clock configuration controls outside operator mode', () => {
    const { action } = start(clockFixture(), false)
    expect(screen.getByText('PERIOD LENGTHS · MINUTES')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Add overtime period' }))
    expect(screen.getByRole('option', { name: 'Overtime 1' })).toBeInTheDocument()
    expect(action).not.toHaveBeenCalled()
  })

  it.each([0, 121])('rejects an invalid overtime duration of %i minutes without changing the draft', minutes => {
    const { action } = start()
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Overtime minutes' }), { target: { value: String(minutes) } })
    fireEvent.click(screen.getByRole('button', { name: 'Prepare overtime' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Overtime must be between 1 and 120 minutes.')
    expect(screen.getByRole('combobox', { name: 'Current period' })).toHaveValue('3')
    expect(screen.getByRole('textbox', { name: 'Remaining time' })).toHaveValue('0:00')
    expect(action).not.toHaveBeenCalled()
  })

  it('validates the staged overtime against the same remaining-time limit as any configured period', () => {
    const { action } = start()
    fireEvent.click(screen.getByRole('button', { name: 'Prepare overtime' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Remaining time' }), { target: { value: '6:00' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply clock' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Remaining time cannot exceed this period’s duration.')
    expect(action).not.toHaveBeenCalled()
  })

  it('can cancel a staged overtime without sending an action or changing the authoritative snapshot', () => {
    const { state, action, onClose } = start()
    const originalState = structuredClone(state)
    fireEvent.click(screen.getByRole('button', { name: 'Prepare overtime' }))
    fireEvent.click(screen.getByRole('button', { name: 'Close clock controls' }))
    expect(onClose).toHaveBeenCalledOnce()
    expect(action).not.toHaveBeenCalled()
    expect(state).toEqual(originalState)
  })

  it('locks overtime preparation and clock mutations while a server update is pending', () => {
    const { action } = start(clockFixture(), true, true)
    expect(screen.getByRole('spinbutton', { name: 'Overtime minutes' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Prepare overtime' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Apply clock' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Advance period' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Prepare overtime' }))
    expect(action).not.toHaveBeenCalled()
  })

  it('observes the existing ten-period configuration limit', () => {
    start(clockFixture(10, Array.from({ length: 10 }, () => 1200000)))
    expect(screen.queryByRole('button', { name: 'Prepare overtime' })).not.toBeInTheDocument()
  })
})
