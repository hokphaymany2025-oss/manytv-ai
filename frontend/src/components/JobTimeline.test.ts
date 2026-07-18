import { describe, expect, it } from 'vitest'
import { buildTimelineEvents } from './JobTimeline'
import type { JobStatusResponse } from '../types'

const baseJob: JobStatusResponse = {
  id: 'job-1',
  kind: 'generate_script',
  status: 'queued',
  project_id: null,
  workflow_name: null,
  result: null,
  error: null,
  shots: null,
  created_at: null,
  started_at: null,
  finished_at: null,
}

describe('buildTimelineEvents', () => {
  it('shows only "Job created" for a freshly queued job with no other timestamps', () => {
    const job = { ...baseJob, created_at: 1.0 }
    expect(buildTimelineEvents(job)).toEqual([{ timestamp: 1.0, label: 'Job created' }])
  })

  it('omits events for null timestamps entirely', () => {
    const job = { ...baseJob, created_at: 1.0, started_at: null, finished_at: null }
    expect(buildTimelineEvents(job)).toHaveLength(1)
  })

  it('includes the terminal "Job {status}" event once finished', () => {
    const job = { ...baseJob, created_at: 1.0, started_at: 2.0, finished_at: 3.0, status: 'done' }
    expect(buildTimelineEvents(job)).toEqual([
      { timestamp: 1.0, label: 'Job created' },
      { timestamp: 2.0, label: 'Job started' },
      { timestamp: 3.0, label: 'Job done' },
    ])
  })

  it('interleaves per-shot submitted/finished events in chronological order', () => {
    const job: JobStatusResponse = {
      ...baseJob,
      kind: 'storyboard',
      status: 'done',
      created_at: 1.0,
      started_at: 2.0,
      finished_at: 5.0,
      shots: [
        {
          shot_index: 0,
          status: 'done',
          prompt_id: 'p0',
          files: [{ filename: 'a.mp4', size_bytes: 1024, content_type: 'video/mp4' }],
          error: null,
          created_at: 2.0,
          submitted_at: 2.5,
          finished_at: 3.5,
        },
        {
          shot_index: 1,
          status: 'done',
          prompt_id: 'p1',
          files: [{ filename: 'b.mp4', size_bytes: 2048, content_type: 'video/mp4' }],
          error: null,
          created_at: 2.0,
          submitted_at: 3.0,
          finished_at: 4.0,
        },
      ],
    }

    expect(buildTimelineEvents(job)).toEqual([
      { timestamp: 1.0, label: 'Job created' },
      { timestamp: 2.0, label: 'Job started' },
      { timestamp: 2.5, label: 'Shot 0 submitted' },
      { timestamp: 3.0, label: 'Shot 1 submitted' },
      { timestamp: 3.5, label: 'Shot 0 done' },
      { timestamp: 4.0, label: 'Shot 1 done' },
      { timestamp: 5.0, label: 'Job done' },
    ])
  })

  it('excludes shot.created_at as a separate event', () => {
    const job: JobStatusResponse = {
      ...baseJob,
      kind: 'storyboard',
      created_at: 1.0,
      shots: [
        {
          shot_index: 0,
          status: 'pending',
          prompt_id: null,
          files: null,
          error: null,
          created_at: 1.5,
          submitted_at: null,
          finished_at: null,
        },
      ],
    }

    expect(buildTimelineEvents(job)).toEqual([{ timestamp: 1.0, label: 'Job created' }])
  })

  it('returns an empty list when every timestamp is null', () => {
    expect(buildTimelineEvents(baseJob)).toEqual([])
  })
})
