import type {
  JobLogEntry,
  JobStatusResponse,
  ScriptGenerationRequest,
  ScriptGenerationResponse,
  StoryboardRequest,
  StoryboardResponse,
} from './types'

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'

// Carries the HTTP status so callers can distinguish e.g. "job genuinely
// doesn't exist" (404) from a transient failure, rather than string-matching
// the message -- first needed by useJob's not-found handling.
export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!response.ok) {
    const body = await response.text()
    throw new ApiError(response.status, `${response.status} ${response.statusText}: ${body}`)
  }
  return response.json() as Promise<T>
}

export function submitScript(body: ScriptGenerationRequest): Promise<ScriptGenerationResponse> {
  return request('/api/generate-script', { method: 'POST', body: JSON.stringify(body) })
}

export function submitStoryboard(body: StoryboardRequest): Promise<StoryboardResponse> {
  return request('/api/storyboard', { method: 'POST', body: JSON.stringify(body) })
}

export function getJob(jobId: string): Promise<JobStatusResponse> {
  return request(`/api/jobs/${jobId}`)
}

export function listJobs(status?: string): Promise<JobStatusResponse[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  return request(`/api/jobs${query}`)
}

export function retryJob(jobId: string): Promise<JobStatusResponse> {
  return request(`/api/jobs/${jobId}/retry`, { method: 'POST' })
}

export function cancelJob(jobId: string): Promise<JobStatusResponse> {
  return request(`/api/jobs/${jobId}/cancel`, { method: 'POST' })
}

export function getJobLogs(jobId: string): Promise<JobLogEntry[]> {
  return request(`/api/jobs/${jobId}/logs`)
}

// ArtifactResponse.filename is already a clean basename (backend/api/routes/
// storyboard.py's _artifact_from_path strips the raw local path server-side)
// -- no client-side path-splitting needed here anymore.
export function downloadUrl(jobId: string, shotIndex: number, filename: string): string {
  return `${API_BASE_URL}/api/jobs/${jobId}/files/${shotIndex}/${encodeURIComponent(filename)}`
}

// SSE URL builders -- EventSource takes a plain URL, not a fetch() call, so
// these mirror downloadUrl's "just build the string" shape rather than
// request()'s fetch-and-parse one.
export function jobEventsUrl(jobId: string): string {
  return `${API_BASE_URL}/api/jobs/${jobId}/events`
}

export function jobLogsEventsUrl(jobId: string): string {
  return `${API_BASE_URL}/api/jobs/${jobId}/logs/events`
}

export function allJobsEventsUrl(): string {
  return `${API_BASE_URL}/api/jobs/events`
}
