import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useJobList } from './useJobList'
import type { JobStatusResponse } from './types'

vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return { ...actual, listJobs: vi.fn() }
})

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
  created_at: 1.0,
  started_at: null,
  finished_at: null,
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
  created_at: 1.0,
  started_at: 1.5,
  finished_at: 2.0,
}

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

  emit(type: string, data: unknown = {}) {
    const event = { data: JSON.stringify(data) } as MessageEvent<string>
    for (const listener of this.listeners[type] ?? []) listener(event)
  }

  close() {
    this.closed = true
  }
}

beforeEach(() => {
  vi.mocked(listJobs).mockReset()
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useJobList', () => {
  it('fetches once on mount and populates jobs', async () => {
    vi.mocked(listJobs).mockResolvedValue([jobA, jobB])

    const { result } = renderHook(() => useJobList())

    await waitFor(() => expect(result.current.jobs).toEqual([jobA, jobB]))
    expect(listJobs).toHaveBeenCalledTimes(1)
  })

  it('opens one persistent SSE connection to the all-jobs stream', async () => {
    vi.mocked(listJobs).mockResolvedValue([jobA])

    renderHook(() => useJobList())

    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    expect(FakeEventSource.instances[0].url).toContain('/api/jobs/events')
  })

  it('re-fetches when a job_updated SSE event arrives', async () => {
    vi.mocked(listJobs).mockResolvedValue([jobA])

    const { result } = renderHook(() => useJobList())
    await waitFor(() => expect(result.current.jobs).toEqual([jobA]))

    vi.mocked(listJobs).mockResolvedValue([jobADone])
    act(() => {
      FakeEventSource.instances[0].emit('job_updated')
    })

    await waitFor(() => expect(result.current.jobs).toEqual([jobADone]))
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

  it('setStatus triggers an immediate re-fetch with that status filter', async () => {
    vi.mocked(listJobs).mockResolvedValue([jobA, jobB])

    const { result } = renderHook(() => useJobList())
    await waitFor(() => expect(listJobs).toHaveBeenCalledWith(undefined))

    vi.mocked(listJobs).mockResolvedValue([jobB])
    act(() => {
      result.current.setStatus('failed')
    })

    await waitFor(() => expect(listJobs).toHaveBeenLastCalledWith('failed'))
    await waitFor(() => expect(result.current.jobs).toEqual([jobB]))
  })

  it('switching the filter back to "All" re-fetches with no filter', async () => {
    vi.mocked(listJobs).mockResolvedValue([jobA, jobB])

    const { result } = renderHook(() => useJobList())
    await waitFor(() => expect(listJobs).toHaveBeenCalledWith(undefined))

    act(() => {
      result.current.setStatus('failed')
    })
    await waitFor(() => expect(listJobs).toHaveBeenLastCalledWith('failed'))

    act(() => {
      result.current.setStatus('')
    })
    await waitFor(() => expect(listJobs).toHaveBeenLastCalledWith(undefined))
  })

  it('an SSE event after the filter changes re-fetches with the new filter, without reopening the connection', async () => {
    vi.mocked(listJobs).mockResolvedValue([jobA])

    const { result } = renderHook(() => useJobList())
    await waitFor(() => expect(listJobs).toHaveBeenLastCalledWith(undefined))

    vi.mocked(listJobs).mockResolvedValue([jobB])
    act(() => {
      result.current.setStatus('failed')
    })
    await waitFor(() => expect(listJobs).toHaveBeenLastCalledWith('failed'))

    expect(FakeEventSource.instances).toHaveLength(1) // not reopened by the filter change

    vi.mocked(listJobs).mockResolvedValue([jobB, jobADone])
    act(() => {
      FakeEventSource.instances[0].emit('job_updated')
    })

    await waitFor(() => expect(listJobs).toHaveBeenLastCalledWith('failed'))
    expect(FakeEventSource.instances).toHaveLength(1)
  })
})
