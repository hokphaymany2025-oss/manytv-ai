import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, getJob, getJobLogs, listJobs } from './api'
import type { JobStatusResponse } from './types'

const sampleJob: JobStatusResponse = {
  id: 'job-1',
  kind: 'storyboard',
  status: 'done',
  project_id: null,
  workflow_name: 'default_t2v',
  result: null,
  error: null,
  shots: null,
  created_at: 1.0,
  started_at: 2.0,
  finished_at: 3.0,
}

describe('listJobs', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('calls GET /api/jobs with no query string when no status is given', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [sampleJob],
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await listJobs()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toBe('http://127.0.0.1:8000/api/jobs')
    expect(result).toEqual([sampleJob])
  })

  it('appends ?status=... when a status filter is given', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [],
    })
    vi.stubGlobal('fetch', fetchMock)

    await listJobs('failed,cancelled')

    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toBe('http://127.0.0.1:8000/api/jobs?status=failed%2Ccancelled')
  })

  it('throws with response detail when the request fails', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      text: async () => 'boom',
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(listJobs()).rejects.toThrow('500 Internal Server Error: boom')
  })
})

describe('getJob', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('throws an ApiError carrying the response status on a 404', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      statusText: 'Not Found',
      text: async () => '{"detail":"Job not found."}',
    })
    vi.stubGlobal('fetch', fetchMock)

    const error = await getJob('does-not-exist').catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(404)
  })
})

describe('getJobLogs', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('calls GET /api/jobs/:id/logs', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [
        { timestamp: 1.0, level: 'info', stage: 'job', message: 'Starting job job-1.', shot_index: null },
      ],
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await getJobLogs('job-1')

    const [url] = fetchMock.mock.calls[0] as [string]
    expect(url).toBe('http://127.0.0.1:8000/api/jobs/job-1/logs')
    expect(result).toEqual([
      { timestamp: 1.0, level: 'info', stage: 'job', message: 'Starting job job-1.', shot_index: null },
    ])
  })
})
