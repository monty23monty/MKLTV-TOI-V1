import { afterEach, describe, expect, it, vi } from 'vitest'
import { createUuid } from './uuid'

afterEach(() => vi.unstubAllGlobals())

describe('request identifiers', () => {
  it('uses the browser UUID implementation when available', () => {
    const randomUUID = vi.fn(() => '566493ac-d44e-4765-8f2e-d58146ac3ed5')
    vi.stubGlobal('crypto', { randomUUID })
    expect(createUuid()).toBe('566493ac-d44e-4765-8f2e-d58146ac3ed5')
    expect(randomUUID).toHaveBeenCalledOnce()
  })

  it('generates correctly versioned, unique UUIDs without secure-context randomUUID', () => {
    const getRandomValues = globalThis.crypto.getRandomValues.bind(globalThis.crypto)
    vi.stubGlobal('crypto', { getRandomValues })
    const ids = Array.from({ length: 100 }, createUuid)
    expect(new Set(ids).size).toBe(100)
    for (const id of ids) expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  })

  it('keeps all random bits except the required UUID version and variant', () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => { bytes.fill(255); return bytes })
    vi.stubGlobal('crypto', { getRandomValues })
    expect(createUuid()).toBe('ffffffff-ffff-4fff-bfff-ffffffffffff')
    expect(getRandomValues).toHaveBeenCalledOnce()
  })

  it('fails visibly instead of producing predictable IDs when secure randomness is absent', () => {
    vi.stubGlobal('crypto', undefined)
    expect(createUuid).toThrow('cannot generate secure request identifiers')
  })
})
