// Mirrors backend/models/schemas.py -- keep in sync with that file by hand,
// there's no shared schema/codegen between the two yet.

export interface ScriptGenerationRequest {
  prompt: string
  target_duration_seconds?: number
  tone?: string
  project_id?: string
}

export interface ScriptGenerationResponse {
  job_id: string
  status: string
}

export interface StoryboardRequest {
  script: string
  workflow_name?: string
  project_id?: string
}

export interface StoryboardResponse {
  job_id: string
  status: string
  shot_count: number
}

export interface ShotStatusResponse {
  shot_index: number
  status: string
  prompt_id: string | null
  files: string[] | null
  error: string | null
  created_at: number | null
  submitted_at: number | null
  finished_at: number | null
}

export interface JobStatusResponse {
  id: string
  kind: string
  status: string
  project_id: string | null
  workflow_name: string | null
  result: Record<string, unknown> | null
  error: string | null
  shots: ShotStatusResponse[] | null
  created_at: number | null
  started_at: number | null
  finished_at: number | null
}

export interface JobLogEntry {
  timestamp: number
  level: string
  stage: string
  message: string
  shot_index: number | null
}
