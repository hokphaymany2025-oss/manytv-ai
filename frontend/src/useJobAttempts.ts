import { useCallback, useEffect, useState } from 'react'
import { getJobAttempts } from './api'
import type { JobAttemptEntry } from './types'

// REST-only, no SSE -- unlike useJobLogs, attempts change at most once per
// retry (far rarer than logs/progress), so a dedicated push channel isn't
// worth it (same reasoning the backend's GET /api/jobs/{id}/attempts route
// used to skip an SSE stream). No 404 handling here (unlike useJob) --
// JobDetailPage only ever mounts this after useJob has already confirmed
// the job exists.
export function useJobAttempts(jobId: string) {
  const [attempts, setAttempts] = useState<JobAttemptEntry[]>([])

  const refresh = useCallback(async () => {
    try {
      setAttempts(await getJobAttempts(jobId))
    } catch {
      // Transient failure -- keep the last-known attempts, matching
      // useJob/useJobList/useJobLogs's resilience to a single failed fetch.
    }
  }, [jobId])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return { attempts, refresh }
}
