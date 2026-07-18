import { useCallback, useEffect, useState } from 'react'
import { getJobLogs, jobLogsEventsUrl } from './api'
import type { JobLogEntry } from './types'

// No 404 handling here (unlike useJob) -- JobDetailPage only ever mounts
// this after useJob has already confirmed the job exists, so there's no
// separate not-found state worth duplicating.
//
// SSE-driven: each `log_added` message is one new JobLogEntry, appended
// in place (the backend only ever sends the delta since the last message
// this connection has seen -- see backend/api/routes/storyboard.py's
// _job_logs_events_stream -- so appending, not replacing, is correct here).
// EventSource reconnects automatically on drop and resends Last-Event-ID,
// so a reconnect resumes from exactly where this tab left off rather than
// re-sending (and re-appending, duplicating) the whole backlog.
export function useJobLogs(jobId: string) {
  const [logs, setLogs] = useState<JobLogEntry[]>([])

  // Kept as a real, working manual full re-fetch (unchanged from the prior
  // polling-based implementation) -- no longer called on a timer, but still
  // useful standalone and preserves this hook's existing public shape.
  const refresh = useCallback(async () => {
    try {
      setLogs(await getJobLogs(jobId))
    } catch {
      // Transient failure -- keep the last-known logs, matching
      // useJob/useJobList's resilience to a single failed poll.
    }
  }, [jobId])

  useEffect(() => {
    setLogs([])
    const eventSource = new EventSource(jobLogsEventsUrl(jobId))
    eventSource.addEventListener('log_added', (event: MessageEvent<string>) => {
      const entry = JSON.parse(event.data) as JobLogEntry
      setLogs((prev) => [...prev, entry])
    })
    return () => eventSource.close()
  }, [jobId])

  return { logs, refresh }
}
