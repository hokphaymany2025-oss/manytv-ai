import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { JobExecutionLogs } from './JobExecutionLogs'
import type { JobLogEntry } from '../types'

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
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('JobExecutionLogs', () => {
  it('shows an empty-state message when there are no log entries yet', async () => {
    render(<JobExecutionLogs jobId="job-1" />)

    await waitFor(() => expect(screen.getByText('No log entries yet.')).toBeTruthy())
  })

  it('renders a job-level entry with its level class and no shot badge', async () => {
    const entry: JobLogEntry = {
      timestamp: 1.0, level: 'info', stage: 'job', message: 'Starting job job-1.', shot_index: null,
    }

    render(<JobExecutionLogs jobId="job-1" />)
    act(() => {
      FakeEventSource.instances[0].emit('log_added', entry)
    })

    const item = await screen.findByText('Starting job job-1.')
    const row = item.closest('li')

    expect(row?.className).toContain('job-logs__entry--info')
    expect(row?.className).not.toContain('job-logs__entry--shot')
  })

  it('renders a shot-tagged entry with a shot badge and error styling', async () => {
    const entry: JobLogEntry = { timestamp: 1.0, level: 'error', stage: 'shot', message: 'boom', shot_index: 2 }

    render(<JobExecutionLogs jobId="job-1" />)
    act(() => {
      FakeEventSource.instances[0].emit('log_added', entry)
    })

    const item = await screen.findByText('boom')
    const row = item.closest('li')

    expect(row?.className).toContain('job-logs__entry--error')
    expect(row?.className).toContain('job-logs__entry--shot')
    expect(screen.getByText('shot 2')).toBeTruthy()
  })
})
