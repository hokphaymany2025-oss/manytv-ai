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
  created_at: 1.0,
  started_at: null,
  finished_at: null,
}

const jobADone: JobStatusResponse = { ...jobA, status: 'done', result: { script: 'hi' } }

class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  closed = false
  private listeners: Record<string, ((event: MessageEvent<string>) => void)[]> = {}

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    ;(this.listeners[type] ??= []).push(listener)
  }

  emit(type: string, data: unknown) {
    const event = { data: JSON.stringify(data) } as MessageEvent<string>
    for (const listener of this.listeners[type] ?? []) listener(event)
  }

  close() {
    this.closed = true
  }
}

beforeEach(() => {
  vi.mocked(getJob).mockReset()
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useJob', () => {
  it('fetches once on mount and populates job, then opens an SSE connection', async () => {
    vi.mocked(getJob).mockResolvedValue(jobA)

    const { result } = renderHook(() => useJob('job-a'))

    await waitFor(() => expect(result.current.job).toEqual(jobA))
    expect(getJob).toHaveBeenCalledWith('job-a')
    expect(result.current.notFound).toBe(false)
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.instances[0].url).toContain('/api/jobs/job-a/events')
  })

  it('updates job on a job_updated SSE event', async () => {
    vi.mocked(getJob).mockResolvedValue(jobA)

    const { result } = renderHook(() => useJob('job-a'))
    await waitFor(() => expect(result.current.job).toEqual(jobA))

    act(() => {
      FakeEventSource.instances[0].emit('job_updated', jobADone)
    })

    await waitFor(() => expect(result.current.job).toEqual(jobADone))
  })

  it('closes the SSE connection on unmount', async () => {
    vi.mocked(getJob).mockResolvedValue(jobA)

    const { unmount } = renderHook(() => useJob('job-a'))
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))

    unmount()

    expect(FakeEventSource.instances[0].closed).toBe(true)
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

  it('sets notFound on a 404 and never opens an SSE connection', async () => {
    vi.mocked(getJob).mockRejectedValue(new ApiError(404, '404 Not Found: {"detail":"Job not found."}'))

    const { result } = renderHook(() => useJob('does-not-exist'))

    await waitFor(() => expect(result.current.notFound).toBe(true))
    expect(result.current.job).toBeNull()
    expect(FakeEventSource.instances).toHaveLength(0)
  })
})
