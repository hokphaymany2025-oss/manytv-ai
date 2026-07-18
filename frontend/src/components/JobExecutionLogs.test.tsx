import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { JobExecutionLogs } from './JobExecutionLogs'
import type { JobLogEntry } from '../types'

vi.mock('../api', () => ({
  getJobLogs: vi.fn(),
}))

import { getJobLogs } from '../api'

beforeEach(() => {
  vi.mocked(getJobLogs).mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('JobExecutionLogs', () => {
  it('shows an empty-state message when there are no log entries yet', async () => {
    vi.mocked(getJobLogs).mockResolvedValue([])

    render(<JobExecutionLogs jobId="job-1" />)

    await waitFor(() => expect(screen.getByText('No log entries yet.')).toBeTruthy())
  })

  it('renders a job-level entry with its level class and no shot badge', async () => {
    const entries: JobLogEntry[] = [
      { timestamp: 1.0, level: 'info', stage: 'job', message: 'Starting job job-1.', shot_index: null },
    ]
    vi.mocked(getJobLogs).mockResolvedValue(entries)

    render(<JobExecutionLogs jobId="job-1" />)

    const item = await screen.findByText('Starting job job-1.')
    const row = item.closest('li')

    expect(row?.className).toContain('job-logs__entry--info')
    expect(row?.className).not.toContain('job-logs__entry--shot')
  })

  it('renders a shot-tagged entry with a shot badge and error styling', async () => {
    const entries: JobLogEntry[] = [
      { timestamp: 1.0, level: 'error', stage: 'shot', message: 'boom', shot_index: 2 },
    ]
    vi.mocked(getJobLogs).mockResolvedValue(entries)

    render(<JobExecutionLogs jobId="job-1" />)

    const item = await screen.findByText('boom')
    const row = item.closest('li')

    expect(row?.className).toContain('job-logs__entry--error')
    expect(row?.className).toContain('job-logs__entry--shot')
    expect(screen.getByText('shot 2')).toBeTruthy()
  })
})
