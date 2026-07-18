import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { classifyArtifact, JobArtifacts } from './JobArtifacts'
import type { JobStatusResponse } from '../types'

describe('classifyArtifact', () => {
  it('classifies video content types', () => {
    expect(classifyArtifact('video/mp4')).toBe('video')
  })

  it('classifies image content types', () => {
    expect(classifyArtifact('image/png')).toBe('image')
  })

  it('classifies anything else as other', () => {
    expect(classifyArtifact('application/octet-stream')).toBe('other')
  })

  it('classifies a null content type as other', () => {
    expect(classifyArtifact(null)).toBe('other')
  })
})

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

describe('JobArtifacts', () => {
  it('shows an empty-state message when there are no artifacts yet', () => {
    render(<JobArtifacts job={baseJob} />)

    expect(screen.getByText('No output artifacts yet.')).toBeTruthy()
  })

  it('renders a video preview, size, and download link for a video artifact', () => {
    const job: JobStatusResponse = {
      ...baseJob,
      shots: [
        {
          shot_index: 0,
          status: 'done',
          prompt_id: 'p0',
          error: null,
          created_at: 1.0,
          submitted_at: 1.0,
          finished_at: 2.0,
          files: [{ filename: 'shot0.mp4', size_bytes: 2048, content_type: 'video/mp4' }],
        },
      ],
    }

    render(<JobArtifacts job={job} />)

    const video = document.querySelector('video')
    expect(video).not.toBeNull()
    expect(video?.getAttribute('src')).toContain('/jobs/job-1/files/0/shot0.mp4')
    expect(screen.getByText('shot0.mp4')).toBeTruthy()
    expect(screen.getByText('2.0 KB')).toBeTruthy()
    expect(screen.getByText('Download')).toBeTruthy()
  })

  it('renders an image preview for an image artifact', () => {
    const job: JobStatusResponse = {
      ...baseJob,
      shots: [
        {
          shot_index: 0,
          status: 'done',
          prompt_id: 'p0',
          error: null,
          created_at: 1.0,
          submitted_at: 1.0,
          finished_at: 2.0,
          files: [{ filename: 'frame.png', size_bytes: 1024, content_type: 'image/png' }],
        },
      ],
    }

    render(<JobArtifacts job={job} />)

    expect(document.querySelector('img')).not.toBeNull()
    expect(document.querySelector('video')).toBeNull()
  })

  it('renders only a download link for a non-previewable artifact', () => {
    const job: JobStatusResponse = {
      ...baseJob,
      shots: [
        {
          shot_index: 0,
          status: 'done',
          prompt_id: 'p0',
          error: null,
          created_at: 1.0,
          submitted_at: 1.0,
          finished_at: 2.0,
          files: [{ filename: 'data.bin', size_bytes: null, content_type: null }],
        },
      ],
    }

    render(<JobArtifacts job={job} />)

    expect(document.querySelector('video')).toBeNull()
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByText('unknown size')).toBeTruthy()
    expect(screen.getByText('Download')).toBeTruthy()
  })
})
