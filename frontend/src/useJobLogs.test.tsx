import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useJobLogs } from './useJobLogs'
import type { JobLogEntry } from './types'

vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return { ...actual, getJobLogs: vi.fn() }
})

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
  vi.mocked(getJobLogs).mockReset()
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useJobLogs', () => {
  it('opens an EventSource to this job\'s logs stream on mount', () => {
    renderHook(() => useJobLogs('job-1'))

    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.instances[0].url).toContain('/api/jobs/job-1/logs/events')
  })

  it('appends each log_added event to the logs array in order', async () => {
    const { result } = renderHook(() => useJobLogs('job-1'))
    const source = FakeEventSource.instances[0]

    act(() => {
      source.emit('log_added', logA)
    })
    await waitFor(() => expect(result.current.logs).toEqual([logA]))

    act(() => {
      source.emit('log_added', logB)
    })
    await waitFor(() => expect(result.current.logs).toEqual([logA, logB]))
  })

  it('resets logs and closes the previous connection when jobId changes', async () => {
    const { result, rerender } = renderHook(({ jobId }) => useJobLogs(jobId), {
      initialProps: { jobId: 'job-1' },
    })
    const firstSource = FakeEventSource.instances[0]
    act(() => {
      firstSource.emit('log_added', logA)
    })
    await waitFor(() => expect(result.current.logs).toEqual([logA]))

    rerender({ jobId: 'job-2' })

    expect(firstSource.closed).toBe(true)
    expect(result.current.logs).toEqual([])
    expect(FakeEventSource.instances).toHaveLength(2)
    expect(FakeEventSource.instances[1].url).toContain('/api/jobs/job-2/logs/events')
  })

  it('refresh() performs a one-shot fetch via the REST endpoint', async () => {
    vi.mocked(getJobLogs).mockResolvedValue([logA, logB])

    const { result } = renderHook(() => useJobLogs('job-1'))
    await act(async () => {
      await result.current.refresh()
    })

    expect(getJobLogs).toHaveBeenCalledWith('job-1')
    expect(result.current.logs).toEqual([logA, logB])
  })

  it('refresh() leaves the previous logs in place when it fails', async () => {
    const { result } = renderHook(() => useJobLogs('job-1'))
    act(() => {
      FakeEventSource.instances[0].emit('log_added', logA)
    })
    await waitFor(() => expect(result.current.logs).toEqual([logA]))

    vi.mocked(getJobLogs).mockRejectedValue(new Error('backend unreachable'))
    await act(async () => {
      await result.current.refresh()
    })

    expect(result.current.logs).toEqual([logA])
  })
})
