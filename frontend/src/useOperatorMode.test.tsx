import { useRef, useState } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useOperatorMode } from './useOperatorMode'

function Harness() {
  const root = useRef<HTMLDivElement>(null)
  const operator = useOperatorMode(root)
  const [selected, setSelected] = useState('89')
  const [pending, setPending] = useState(false)
  const [revision, setRevision] = useState(7)
  return <div id="lightning-toi-operator" ref={root} data-testid="operator-root">
    <button onClick={() => void operator.enter()}>Enter</button>
    <button onClick={() => void operator.exit()}>Exit</button>
    <button onClick={() => void operator.toggle()}>Toggle</button>
    <button onClick={() => { setSelected('19'); setPending(true) }}>Select substitute</button>
    <button onClick={() => { setRevision(8); setPending(false) }}>Server response</button>
    <output data-testid="mode">{operator.mode}</output>
    <output data-testid="active">{String(operator.active)}</output>
    <output data-testid="native">{String(operator.browserFullscreen)}</output>
    <output data-testid="reason">{operator.fallbackReason}</output>
    <output data-testid="tracking">{selected}/{String(pending)}/{revision}</output>
  </div>
}

let fullscreen: Element | null
let request: ReturnType<typeof vi.fn>
let exit: ReturnType<typeof vi.fn>

function nativeChange(element: Element | null) {
  fullscreen = element
  document.dispatchEvent(new Event('fullscreenchange'))
}

function deferred() {
  let resolve!: () => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<void>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

beforeEach(() => {
  fullscreen = null
  Object.defineProperty(document, 'fullscreenElement', { configurable: true, get: () => fullscreen })
  Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, value: true })
  request = vi.fn(function (this: HTMLElement) {
    nativeChange(this)
    return Promise.resolve()
  })
  exit = vi.fn(() => { nativeChange(null); return Promise.resolve() })
  Object.defineProperty(HTMLElement.prototype, 'requestFullscreen', { configurable: true, writable: true, value: request })
  Object.defineProperty(document, 'exitFullscreen', { configurable: true, writable: true, value: exit })
})

describe('useOperatorMode', () => {
  it('enters native fullscreen on the existing root and exits from the visible control', async () => {
    render(<Harness />)
    const root = screen.getByTestId('operator-root')
    const enter = screen.getByRole('button', { name: 'Enter' })
    enter.focus()
    fireEvent.click(enter)
    await waitFor(() => expect(screen.getByTestId('mode').textContent).toBe('browser'))
    expect(request).toHaveBeenCalledTimes(1)
    expect(request.mock.instances[0]).toBe(root)
    expect(screen.getByTestId('native').textContent).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Exit' }))
    await waitFor(() => expect(screen.getByTestId('mode').textContent).toBe('standard'))
    expect(exit).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(enter)
    expect(screen.getByTestId('operator-root')).toBe(root)
  })

  it('synchronises browser-driven exit without changing tracking selection', async () => {
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    await waitFor(() => expect(screen.getByTestId('native').textContent).toBe('true'))
    act(() => nativeChange(null))
    expect(screen.getByTestId('active').textContent).toBe('false')
    expect(screen.getByTestId('tracking').textContent).toBe('89/false/7')
    expect(exit).not.toHaveBeenCalled()
  })

  it('exits native fullscreen with Escape', async () => {
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    await waitFor(() => expect(screen.getByTestId('mode').textContent).toBe('browser'))
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.getByTestId('mode').textContent).toBe('standard'))
    expect(exit).toHaveBeenCalledTimes(1)
  })

  it('uses an explicit focus fallback when the browser rejects fullscreen', async () => {
    request.mockRejectedValueOnce(new DOMException('Permission denied', 'NotAllowedError'))
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    await waitFor(() => expect(screen.getByTestId('reason').textContent).toContain('declined'))
    expect(screen.getByTestId('mode').textContent).toBe('focus')
    expect(screen.getByTestId('active').textContent).toBe('true')
    expect(screen.getByTestId('native').textContent).toBe('false')
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.getByTestId('mode').textContent).toBe('standard')
    expect(exit).not.toHaveBeenCalled()
  })

  it('handles a synchronous rejection without leaving an unresolved entry', async () => {
    request.mockImplementationOnce(() => { throw new DOMException('Denied') })
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    expect(screen.getByTestId('reason').textContent).toContain('declined')
    fireEvent.click(screen.getByRole('button', { name: 'Exit' }))
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    await waitFor(() => expect(screen.getByTestId('mode').textContent).toBe('browser'))
  })

  it.each(['missing API', 'disabled policy'])('offers focus layout for %s', async (scenario) => {
    if (scenario === 'missing API') Object.defineProperty(HTMLElement.prototype, 'requestFullscreen', { configurable: true, value: undefined })
    else Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, value: false })
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    expect(screen.getByTestId('mode').textContent).toBe('focus')
    expect(screen.getByTestId('native').textContent).toBe('false')
    expect(screen.getByTestId('reason').textContent).toContain('unavailable')
    fireEvent.click(screen.getByRole('button', { name: 'Exit' }))
    expect(screen.getByTestId('mode').textContent).toBe('standard')
    expect(exit).not.toHaveBeenCalled()
  })

  it('does not claim native fullscreen when an implementation resolves without entering', async () => {
    request.mockResolvedValueOnce(undefined)
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    await waitFor(() => expect(screen.getByTestId('reason').textContent).toContain('did not start'))
    expect(screen.getByTestId('mode').textContent).toBe('focus')
    expect(screen.getByTestId('native').textContent).toBe('false')
  })

  it('coalesces accidental repeated entry taps while permission is pending', async () => {
    const permission = deferred()
    request.mockReturnValueOnce(permission.promise)
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    expect(request).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('native').textContent).toBe('false')
    await act(async () => { nativeChange(screen.getByTestId('operator-root')); permission.resolve(); await permission.promise })
    expect(screen.getByTestId('mode').textContent).toBe('browser')
  })

  it('cancels a pending request and cleans up a later browser grant', async () => {
    const permission = deferred()
    request.mockReturnValueOnce(permission.promise)
    render(<Harness />)
    const root = screen.getByTestId('operator-root')
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.getByTestId('mode').textContent).toBe('standard')
    await act(async () => { nativeChange(root); permission.resolve(); await permission.promise })
    expect(screen.getByTestId('mode').textContent).toBe('standard')
    expect(fullscreen).toBeNull()
    expect(exit).toHaveBeenCalledTimes(1)
  })

  it('does not resurrect focus layout after a cancelled request later rejects', async () => {
    const permission = deferred()
    request.mockReturnValueOnce(permission.promise)
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    fireEvent.click(screen.getByRole('button', { name: 'Exit' }))
    await act(async () => { permission.reject(new DOMException('Denied')); await permission.promise.catch(() => undefined) })
    expect(screen.getByTestId('mode').textContent).toBe('standard')
    expect(screen.getByTestId('reason').textContent).toBe('')
  })

  it('keeps the browser exit authoritative when the entry promise resolves later', async () => {
    const permission = deferred()
    request.mockReturnValueOnce(permission.promise)
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    act(() => nativeChange(screen.getByTestId('operator-root')))
    expect(screen.getByTestId('mode').textContent).toBe('browser')
    act(() => nativeChange(null))
    await act(async () => { permission.resolve(); await permission.promise })
    expect(screen.getByTestId('mode').textContent).toBe('standard')
    expect(screen.getByTestId('tracking').textContent).toBe('89/false/7')
  })

  it('preserves selected players and pending server state across both layouts', async () => {
    render(<Harness />)
    const root = screen.getByTestId('operator-root')
    fireEvent.click(screen.getByRole('button', { name: 'Select substitute' }))
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    await waitFor(() => expect(screen.getByTestId('native').textContent).toBe('true'))
    expect(screen.getByTestId('tracking').textContent).toBe('19/true/7')
    fireEvent.click(screen.getByRole('button', { name: 'Server response' }))
    fireEvent.click(screen.getByRole('button', { name: 'Exit' }))
    await waitFor(() => expect(screen.getByTestId('active').textContent).toBe('false'))
    expect(screen.getByTestId('tracking').textContent).toBe('19/false/8')
    expect(screen.getByTestId('operator-root')).toBe(root)
  })

  it('reflects actual native fullscreen when exit is rejected and permits browser Escape afterward', async () => {
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    await waitFor(() => expect(screen.getByTestId('mode').textContent).toBe('browser'))
    exit.mockRejectedValueOnce(new DOMException('Busy'))
    fireEvent.click(screen.getByRole('button', { name: 'Exit' }))
    await waitFor(() => expect(screen.getByTestId('reason').textContent).toContain('Escape'))
    expect(screen.getByTestId('native').textContent).toBe('true')
    act(() => nativeChange(null))
    expect(screen.getByTestId('mode').textContent).toBe('standard')
  })

  it('notifies a host layout using the documented explicit window event', async () => {
    const notices: unknown[] = []
    const listener = (event: Event) => notices.push((event as CustomEvent).detail)
    window.addEventListener('lightning-toi:operator-layout', listener)
    const { unmount } = render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Toggle' }))
    await waitFor(() => expect(screen.getByTestId('mode').textContent).toBe('browser'))
    expect(notices).toContainEqual({ active: true, mode: 'browser', browserFullscreen: true, elementId: 'lightning-toi-operator' })
    fireEvent.click(screen.getByRole('button', { name: 'Toggle' }))
    await waitFor(() => expect(screen.getByTestId('mode').textContent).toBe('standard'))
    expect(notices.at(-1)).toEqual({ active: false, mode: 'standard', browserFullscreen: false, elementId: 'lightning-toi-operator' })
    unmount()
    window.removeEventListener('lightning-toi:operator-layout', listener)
  })

  it('cleans up its fullscreen and host layout when unmounted', async () => {
    const { unmount } = render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    await waitFor(() => expect(screen.getByTestId('mode').textContent).toBe('browser'))
    unmount()
    expect(exit).toHaveBeenCalledTimes(1)
    expect(fullscreen).toBeNull()
  })

  it('cleans up a native permission grant received after unmount', async () => {
    const permission = deferred()
    request.mockReturnValueOnce(permission.promise)
    const { unmount } = render(<Harness />)
    const root = screen.getByTestId('operator-root')
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    unmount()
    await act(async () => { nativeChange(root); permission.resolve(); await permission.promise })
    expect(fullscreen).toBeNull()
    expect(exit).toHaveBeenCalledTimes(1)
  })

  it('does not exit another component fullscreen on fallback exit or unmount', async () => {
    const unrelated = document.createElement('section')
    fullscreen = unrelated
    request.mockRejectedValueOnce(new DOMException('Denied'))
    const { unmount } = render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Enter' }))
    await waitFor(() => expect(screen.getByTestId('reason').textContent).toContain('declined'))
    fireEvent.click(screen.getByRole('button', { name: 'Exit' }))
    unmount()
    expect(exit).not.toHaveBeenCalled()
    expect(fullscreen).toBe(unrelated)
  })
})
