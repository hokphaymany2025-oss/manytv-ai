import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { JobRow } from './JobRow'
import type { JobStatusResponse } from '../types'

const baseJob: JobStatusResponse = {
  id: 'job-1',
  kind: 'storyboard',
  status: 'done',
  project_id: null,
  workflow_name: 'default_t2v',
  result: null,
  error: null,
  shots: null,
  created_at: 1.0,
  started_at: 1.0,
  finished_at: 2.0,
}

function renderRow(job: JobStatusResponse) {
  return render(
    <MemoryRouter>
      <ul>
        <JobRow job={job} onChanged={vi.fn()} />
      </ul>
    </MemoryRouter>,
  )
}

describe('JobRow project_id display', () => {
  it('renders the project id badge when present', () => {
    renderRow({ ...baseJob, project_id: 'lighthouse-series' })

    expect(screen.getByText('lighthouse-series')).toBeTruthy()
  })

  it('renders no project id badge when project_id is null', () => {
    renderRow(baseJob)

    expect(screen.queryByText('lighthouse-series')).toBeNull()
    // No stray empty badge element left behind either.
    expect(document.querySelector('.job-row__project')).toBeNull()
  })
})
