import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useJobLogs } from './useJobLogs'
import type { JobLogEntry } from './types'

vi.mock('./api', () => ({
  getJobLogs: vi.fn(),
}))

import { getJobLogs } from './api'

const logA: JobLogEntry = {
  timestamp: 1.0,
  level: 'info',
  stage: 'job',
  message: 'Starting job job-1.',
  shot_index: null,
}

const logB: JobLogEntry = {
  timestamp: 2.0,
  level: 'info',
  stage: 'job',
  message: 'Job job-1 completed in 1.0s.',
  shot_index: null,
}

beforeEach(() => {
  vi.mocked(getJobLogs).mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useJobLogs', () => {
  it('fetches once on mount and populates logs', async () => {
    vi.mocked(getJobLogs).mockResolvedValue([logA])

    const { result } = renderHook(() => useJobLogs('job-1'))

    await waitFor(() => expect(result.current.logs).toEqual([logA]))
    expect(getJobLogs).toHaveBeenCalledWith('job-1')
  })

  it('re-fetches on each poll interval', async () => {
    vi.useFakeTimers()
    vi.mocked(getJobLogs).mockResolvedValue([logA])

    const { result } = renderHook(() => useJobLogs('job-1'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.logs).toEqual([logA])
    expect(getJobLogs).toHaveBeenCalledTimes(1)

    vi.mocked(getJobLogs).mockResolvedValue([logA, logB])
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    expect(result.current.logs).toEqual([logA, logB])
    expect(getJobLogs).toHaveBeenCalledTimes(2)
  })

  it('leaves the previous logs in place when a refresh fails', async () => {
    vi.mocked(getJobLogs).mockResolvedValue([logA])

    const { result } = renderHook(() => useJobLogs('job-1'))
    await waitFor(() => expect(result.current.logs).toEqual([logA]))

    vi.mocked(getJobLogs).mockRejectedValue(new Error('backend unreachable'))
    await act(async () => {
      await result.current.refresh()
    })

    expect(result.current.logs).toEqual([logA])
  })
})
