import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useJobList } from './useJobList'
import type { JobStatusResponse } from './types'

vi.mock('./api', () => ({
  listJobs: vi.fn(),
}))

import { listJobs } from './api'

const jobA: JobStatusResponse = {
  id: 'job-a',
  kind: 'generate_script',
  status: 'queued',
  project_id: null,
  workflow_name: null,
  result: null,
  error: null,
  shots: null,
}

const jobADone: JobStatusResponse = { ...jobA, status: 'done', result: { script: 'hi' } }

const jobB: JobStatusResponse = {
  id: 'job-b',
  kind: 'storyboard',
  status: 'failed',
  project_id: null,
  workflow_name: 'default_t2v',
  result: null,
  error: 'boom',
  shots: [],
}

beforeEach(() => {
  vi.mocked(listJobs).mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useJobList', () => {
  it('fetches once on mount and populates jobs', async () => {
    vi.mocked(listJobs).mockResolvedValue([jobA, jobB])

    const { result } = renderHook(() => useJobList())

    await waitFor(() => expect(result.current.jobs).toEqual([jobA, jobB]))
    expect(listJobs).toHaveBeenCalledTimes(1)
  })

  it('re-fetches on each poll interval', async () => {
    // Fake timers must be active *before* the hook mounts, so the
    // setInterval it registers is one vitest can actually control --
    // waitFor's own real-timer-based polling isn't used in this test for
    // exactly that reason; advanceTimersByTimeAsync(0) flushes the
    // mount-time refresh()'s pending promise instead.
    vi.useFakeTimers()
    vi.mocked(listJobs).mockResolvedValue([jobA])

    const { result } = renderHook(() => useJobList())
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.jobs).toEqual([jobA])
    expect(listJobs).toHaveBeenCalledTimes(1)

    vi.mocked(listJobs).mockResolvedValue([jobADone])
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    expect(result.current.jobs).toEqual([jobADone])
    expect(listJobs).toHaveBeenCalledTimes(2)
  })

  it('updateJob replaces the matching job in place without a refetch', async () => {
    vi.mocked(listJobs).mockResolvedValue([jobA, jobB])

    const { result } = renderHook(() => useJobList())
    await waitFor(() => expect(result.current.jobs).toEqual([jobA, jobB]))

    const callsBefore = vi.mocked(listJobs).mock.calls.length
    act(() => {
      result.current.updateJob(jobADone)
    })

    expect(result.current.jobs).toEqual([jobADone, jobB])
    expect(listJobs).toHaveBeenCalledTimes(callsBefore)
  })

  it('leaves the previous jobs list in place when a refresh fails', async () => {
    vi.mocked(listJobs).mockResolvedValue([jobA])

    const { result } = renderHook(() => useJobList())
    await waitFor(() => expect(result.current.jobs).toEqual([jobA]))

    vi.mocked(listJobs).mockRejectedValue(new Error('backend unreachable'))
    await act(async () => {
      await result.current.refresh()
    })

    expect(result.current.jobs).toEqual([jobA])
  })
})
