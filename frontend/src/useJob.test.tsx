import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useJob } from './useJob'
import type { JobStatusResponse } from './types'
import { ApiError } from './api'

vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return { ...actual, getJob: vi.fn() }
})

import { getJob } from './api'

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

beforeEach(() => {
  vi.mocked(getJob).mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useJob', () => {
  it('fetches once on mount and populates job', async () => {
    vi.mocked(getJob).mockResolvedValue(jobA)

    const { result } = renderHook(() => useJob('job-a'))

    await waitFor(() => expect(result.current.job).toEqual(jobA))
    expect(getJob).toHaveBeenCalledWith('job-a')
    expect(result.current.notFound).toBe(false)
  })

  it('re-fetches on each poll interval', async () => {
    vi.useFakeTimers()
    vi.mocked(getJob).mockResolvedValue(jobA)

    const { result } = renderHook(() => useJob('job-a'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.job).toEqual(jobA)

    vi.mocked(getJob).mockResolvedValue(jobADone)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    expect(result.current.job).toEqual(jobADone)
    expect(getJob).toHaveBeenCalledTimes(2)
  })

  it('leaves the previous job in place when a transient (non-404) fetch fails', async () => {
    vi.mocked(getJob).mockResolvedValue(jobA)

    const { result } = renderHook(() => useJob('job-a'))
    await waitFor(() => expect(result.current.job).toEqual(jobA))

    vi.mocked(getJob).mockRejectedValue(new Error('backend unreachable'))
    await act(async () => {
      await result.current.refresh()
    })

    expect(result.current.job).toEqual(jobA)
    expect(result.current.notFound).toBe(false)
  })

  it('sets notFound on a 404 and stops issuing further requests', async () => {
    vi.useFakeTimers()
    vi.mocked(getJob).mockRejectedValue(new ApiError(404, '404 Not Found: {"detail":"Job not found."}'))

    const { result } = renderHook(() => useJob('does-not-exist'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(result.current.notFound).toBe(true)
    expect(result.current.job).toBeNull()
    const callsAfterNotFound = vi.mocked(getJob).mock.calls.length

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    expect(getJob).toHaveBeenCalledTimes(callsAfterNotFound)
  })
})
