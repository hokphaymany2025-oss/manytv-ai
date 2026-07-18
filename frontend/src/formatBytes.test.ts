import { describe, expect, it } from 'vitest'
import { formatBytes } from './formatBytes'

describe('formatBytes', () => {
  it('shows a placeholder for null', () => {
    expect(formatBytes(null)).toBe('unknown size')
  })

  it('formats zero bytes', () => {
    expect(formatBytes(0)).toBe('0 B')
  })

  it('formats sub-KB sizes as whole bytes', () => {
    expect(formatBytes(512)).toBe('512 B')
  })

  it('formats KB sizes with one decimal', () => {
    expect(formatBytes(2048)).toBe('2.0 KB')
  })

  it('formats MB sizes with one decimal', () => {
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB')
  })

  it('formats GB sizes with one decimal', () => {
    expect(formatBytes(3 * 1024 * 1024 * 1024)).toBe('3.0 GB')
  })

  it('does not go past GB for very large sizes', () => {
    expect(formatBytes(1024 * 1024 * 1024 * 1024)).toBe('1024.0 GB')
  })
})
