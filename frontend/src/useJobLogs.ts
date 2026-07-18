import { useCallback, useEffect, useState } from 'react'
import { getJobLogs } from './api'
import type { JobLogEntry } from './types'

const POLL_INTERVAL_MS = 3000

// No 404 handling here (unlike useJob) -- JobDetailPage only ever mounts
// this after useJob has already confirmed the job exists, so there's no
// separate not-found state worth duplicating.
export function useJobLogs(jobId: string) {
  const [logs, setLogs] = useState<JobLogEntry[]>([])

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
    void refresh()
    const interval = setInterval(() => void refresh(), POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [refresh])

  return { logs, refresh }
}
