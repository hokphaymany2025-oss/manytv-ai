import { describe, expect, it } from 'vitest'
import { formatAbsolute, formatRelative } from './formatTime'

describe('formatAbsolute', () => {
  it('formats a unix timestamp as a locale date/time string', () => {
    const result = formatAbsolute(0)
    expect(typeof result).toBe('string')
    expect(result.length).toBeGreaterThan(0)
  })
})

describe('formatRelative', () => {
  const now = 1_000_000

  it('formats sub-minute differences in seconds', () => {
    expect(formatRelative(now - 30, now)).toBe('30s ago')
  })

  it('formats sub-hour differences in minutes', () => {
    expect(formatRelative(now - 5 * 60, now)).toBe('5m ago')
  })

  it('formats sub-day differences in hours', () => {
    expect(formatRelative(now - 3 * 3600, now)).toBe('3h ago')
  })

  it('formats multi-day differences in days', () => {
    expect(formatRelative(now - 2 * 86400, now)).toBe('2d ago')
  })

  it('clamps negative diffs (clock skew) to 0s ago', () => {
    expect(formatRelative(now + 5, now)).toBe('0s ago')
  })
})
