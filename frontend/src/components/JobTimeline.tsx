import type { JobStatusResponse } from '../types'
import { formatAbsolute, formatRelative } from '../formatTime'

export interface TimelineEvent {
  timestamp: number
  label: string
}

// Current-attempt lifecycle only -- retrying/cancelling a job clears
// started_at/finished_at (and per-shot submitted_at/finished_at), so a past
// attempt before a retry leaves no trace here. Deliberately excludes
// shot.created_at: every shot is bootstrapped in the same synchronous loop
// right after the job starts, so it would just duplicate "Job started".
export function buildTimelineEvents(job: JobStatusResponse): TimelineEvent[] {
  const events: TimelineEvent[] = []

  if (job.created_at != null) events.push({ timestamp: job.created_at, label: 'Job created' })
  if (job.started_at != null) events.push({ timestamp: job.started_at, label: 'Job started' })

  for (const shot of job.shots ?? []) {
    if (shot.submitted_at != null) {
      events.push({ timestamp: shot.submitted_at, label: `Shot ${shot.shot_index} submitted` })
    }
    if (shot.finished_at != null) {
      events.push({ timestamp: shot.finished_at, label: `Shot ${shot.shot_index} ${shot.status}` })
    }
  }

  if (job.finished_at != null) events.push({ timestamp: job.finished_at, label: `Job ${job.status}` })

  return events.sort((a, b) => a.timestamp - b.timestamp)
}

interface Props {
  job: JobStatusResponse
}

export function JobTimeline({ job }: Props) {
  const events = buildTimelineEvents(job)

  if (events.length === 0) return null

  return (
    <ul className="job-timeline">
      {events.map((event, index) => (
        <li key={`${event.timestamp}-${index}`} className="job-timeline__event">
          <span className="job-timeline__label">{event.label}</span>
          <span className="job-timeline__time">
            {formatAbsolute(event.timestamp)} ({formatRelative(event.timestamp)})
          </span>
        </li>
      ))}
    </ul>
  )
}
