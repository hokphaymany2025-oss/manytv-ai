import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { JobAttemptHistory } from './JobAttemptHistory'
import type { JobAttemptEntry } from '../types'

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return { ...actual, getJobAttempts: vi.fn() }
})

import { getJobAttempts } from '../api'

beforeEach(() => {
  vi.mocked(getJobAttempts).mockReset()
})

describe('JobAttemptHistory', () => {
  it('shows an empty-state message when there are no previous attempts', async () => {
    vi.mocked(getJobAttempts).mockResolvedValue([])

    render(<JobAttemptHistory jobId="job-1" />)

    expect(await screen.findByText('No previous attempts.')).toBeTruthy()
  })

  it('renders a failed attempt with its status and error message', async () => {
    const entry: JobAttemptEntry = {
      status: 'failed', error: 'ComfyUI is not reachable', started_at: 10.0, finished_at: 20.0, recorded_at: 30.0,
    }
    vi.mocked(getJobAttempts).mockResolvedValue([entry])

    render(<JobAttemptHistory jobId="job-1" />)

    const item = (await screen.findByText('failed')).closest('li')
    expect(item?.className).toContain('job-attempts__entry--failed')
    expect(screen.getByText('Attempt 1')).toBeTruthy()
    expect(screen.getByText('ComfyUI is not reachable')).toBeTruthy()
  })

  it('renders a cancelled attempt with no error message', async () => {
    const entry: JobAttemptEntry = {
      status: 'cancelled', error: null, started_at: 10.0, finished_at: 20.0, recorded_at: 30.0,
    }
    vi.mocked(getJobAttempts).mockResolvedValue([entry])

    render(<JobAttemptHistory jobId="job-1" />)

    const item = (await screen.findByText('cancelled')).closest('li')
    expect(item?.className).toContain('job-attempts__entry--cancelled')
    expect(item?.querySelector('.job-attempts__error')).toBeNull()
  })
})
