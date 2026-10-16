import { useCallback, useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'

export type OperatorLayoutMode = 'standard' | 'browser' | 'focus'

interface OperatorLayout {
  mode: OperatorLayoutMode
  browserFullscreen: boolean
  fallbackReason: string | null
}

const standardLayout: OperatorLayout = { mode: 'standard', browserFullscreen: false, fallbackReason: null }

async function leaveOwnFullscreen(root: HTMLElement) {
  if (document.fullscreenElement !== root || typeof document.exitFullscreen !== 'function') return
  try {
    await document.exitFullscreen()
  } catch {
    // Cleanup must remain safe if the document is no longer active.
  }
}

/** Presentation state only: the same mounted operator retains all match state. */
export function useOperatorMode(rootRef: RefObject<HTMLElement | null>) {
  const [layout, setLayout] = useState<OperatorLayout>(standardLayout)
  const mounted = useRef(true)
  const requested = useRef(false)
  const nativeEntered = useRef(false)
  const generation = useRef(0)
  const entering = useRef<Promise<void> | null>(null)
  const exiting = useRef<Promise<void> | null>(null)
  const returnFocus = useRef<HTMLElement | null>(null)

  const update = useCallback((next: OperatorLayout) => {
    if (mounted.current) setLayout(next)
  }, [])

  const restoreFocus = useCallback(() => {
    const target = returnFocus.current
    if (target?.isConnected) target.focus({ preventScroll: true })
    returnFocus.current = null
  }, [])

  const finishExit = useCallback(() => {
    requested.current = false
    nativeEntered.current = false
    update(standardLayout)
    restoreFocus()
  }, [restoreFocus, update])

  const exit = useCallback((): Promise<void> => {
    if (exiting.current) return exiting.current
    generation.current += 1
    requested.current = false
    const root = rootRef.current
    if (!root || document.fullscreenElement !== root) {
      finishExit()
      return Promise.resolve()
    }

    // Keep reflecting real browser fullscreen until its exit event arrives.
    let result: Promise<void>
    try {
      result = document.exitFullscreen()
    } catch {
      requested.current = true
      update({ mode: 'browser', browserFullscreen: true, fallbackReason: 'Use Escape or your browser controls to exit fullscreen.' })
      return Promise.resolve()
    }
    const operation = Promise.resolve(result).then(() => {
      if (!mounted.current) return
      if (document.fullscreenElement === root) {
        requested.current = true
        update({ mode: 'browser', browserFullscreen: true, fallbackReason: 'Use Escape or your browser controls to exit fullscreen.' })
      } else {
        finishExit()
      }
    }, () => {
      if (!mounted.current) return
      if (document.fullscreenElement === root) {
        requested.current = true
        update({ mode: 'browser', browserFullscreen: true, fallbackReason: 'Use Escape or your browser controls to exit fullscreen.' })
      } else {
        finishExit()
      }
    }).finally(() => {
      if (exiting.current === operation) exiting.current = null
    })
    exiting.current = operation
    return operation
  }, [finishExit, rootRef, update])

  const enter = useCallback((): Promise<void> => {
    if (requested.current) return entering.current ?? Promise.resolve()
    if (exiting.current) return exiting.current.then(() => enter())
    const root = rootRef.current
    if (!root) return Promise.resolve()
    const token = ++generation.current
    requested.current = true
    nativeEntered.current = false
    returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    update({ mode: 'focus', browserFullscreen: false, fallbackReason: null })

    if (document.fullscreenElement === root) {
      nativeEntered.current = true
      update({ mode: 'browser', browserFullscreen: true, fallbackReason: null })
      return Promise.resolve()
    }
    if (typeof root.requestFullscreen !== 'function' || document.fullscreenEnabled === false) {
      update({ mode: 'focus', browserFullscreen: false, fallbackReason: 'Browser fullscreen is unavailable. Focus layout is active.' })
      return Promise.resolve()
    }

    let result: Promise<void>
    try {
      // Call directly within the operator's gesture to retain browser activation.
      result = root.requestFullscreen()
    } catch {
      update({ mode: 'focus', browserFullscreen: false, fallbackReason: 'Browser fullscreen was declined. Focus layout is active.' })
      return Promise.resolve()
    }
    const operation = Promise.resolve(result).then(async () => {
      if (!mounted.current || token !== generation.current || !requested.current) {
        // A delayed permission result must not re-enter after Escape or unmount.
        if ((!mounted.current || !requested.current) && document.fullscreenElement === root) {
          await (exiting.current ?? leaveOwnFullscreen(root))
        }
        return
      }
      if (document.fullscreenElement === root) {
        nativeEntered.current = true
        update({ mode: 'browser', browserFullscreen: true, fallbackReason: null })
      } else {
        update({ mode: 'focus', browserFullscreen: false, fallbackReason: 'Fullscreen did not start. Focus layout is active.' })
      }
    }, () => {
      if (mounted.current && token === generation.current && requested.current) {
        if (document.fullscreenElement === root) {
          nativeEntered.current = true
          update({ mode: 'browser', browserFullscreen: true, fallbackReason: null })
        } else {
          update({ mode: 'focus', browserFullscreen: false, fallbackReason: 'Browser fullscreen was declined. Focus layout is active.' })
        }
      }
    }).finally(() => {
      if (entering.current === operation) entering.current = null
    })
    entering.current = operation
    return operation
  }, [rootRef, update])

  const toggle = useCallback(() => requested.current ? exit() : enter(), [enter, exit])

  useEffect(() => {
    window.dispatchEvent(new CustomEvent('lightning-toi:operator-layout', {
      detail: {
        active: layout.mode !== 'standard',
        mode: layout.mode,
        browserFullscreen: layout.browserFullscreen,
        elementId: 'lightning-toi-operator',
      },
    }))
  }, [layout.mode, layout.browserFullscreen])

  useEffect(() => {
    mounted.current = true
    const root = rootRef.current
    const fullscreenChanged = () => {
      if (document.fullscreenElement === root && root) {
        if (requested.current) {
          nativeEntered.current = true
          update({ mode: 'browser', browserFullscreen: true, fallbackReason: null })
        } else {
          void exit()
        }
      } else if (nativeEntered.current) {
        generation.current += 1
        finishExit()
      }
    }
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && requested.current) {
        event.preventDefault()
        void exit()
      }
    }
    document.addEventListener('fullscreenchange', fullscreenChanged)
    window.addEventListener('keydown', keydown)
    return () => {
      mounted.current = false
      requested.current = false
      generation.current += 1
      document.removeEventListener('fullscreenchange', fullscreenChanged)
      window.removeEventListener('keydown', keydown)
      if (root && document.fullscreenElement === root) void leaveOwnFullscreen(root)
      window.dispatchEvent(new CustomEvent('lightning-toi:operator-layout', {
        detail: { active: false, mode: 'standard', browserFullscreen: false, elementId: 'lightning-toi-operator' },
      }))
    }
  }, [exit, finishExit, rootRef, update])

  return { ...layout, active: layout.mode !== 'standard', enter, exit, toggle }
}
