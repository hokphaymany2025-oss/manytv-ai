import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, getJob } from './api'
import type { JobStatusResponse } from './types'

const POLL_INTERVAL_MS = 3000

export function useJob(jobId: string) {
  const [job, setJob] = useState<JobStatusResponse | null>(null)
  const [notFound, setNotFound] = useState(false)
  // A confirmed 404 will never resolve on a later poll (jobs are never
  // deleted) -- a ref (not state) so refresh's own closure can see the
  // latest value synchronously without retriggering the effect below.
  const notFoundRef = useRef(false)

  const refresh = useCallback(async () => {
    if (notFoundRef.current) return
    try {
      setJob(await getJob(jobId))
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        notFoundRef.current = true
        setNotFound(true)
        return
      }
      // Any other failure (backend unreachable, transient error) -- leave
      // the last-known job in place rather than clearing it.
    }
  }, [jobId])

  useEffect(() => {
    notFoundRef.current = false
    setNotFound(false)
    setJob(null)
    void refresh()
    const interval = setInterval(() => void refresh(), POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [refresh])

  return { job, notFound, refresh, updateJob: setJob }
}
