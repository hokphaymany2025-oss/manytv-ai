import { useCallback, useEffect, useRef, useState } from 'react'
import { allJobsEventsUrl, listJobs } from './api'
import type { JobStatusResponse } from './types'

export function useJobList() {
  const [jobs, setJobs] = useState<JobStatusResponse[]>([])
  const [status, setStatus] = useState('')

  const refresh = useCallback(async () => {
    try {
      setJobs(await listJobs(status || undefined))
    } catch {
      // Backend unreachable/transient -- leave the last-known list in
      // place rather than clearing it.
    }
  }, [status])

  // Always holds the latest refresh (i.e. the one respecting whatever
  // status filter is currently active) -- read by the SSE effect below,
  // which deliberately only runs once (see that effect's own comment).
  const refreshRef = useRef(refresh)
  refreshRef.current = refresh

  useEffect(() => {
    void refresh()
  }, [refresh])

  // One persistent connection for the whole component lifetime, independent
  // of the status filter: the backend's all-jobs stream is deliberately
  // unfiltered and signal-only (see backend/api/routes/storyboard.py's
  // _all_jobs_events_stream) -- a status-filtered view can both gain and
  // lose jobs as they change, and only a full re-fetch through the
  // existing, already-correct GET /api/jobs?status= call (not a per-job
  // upsert) handles a job leaving the filtered view correctly. Every
  // message here just re-runs whichever refresh() is current via
  // refreshRef, so changing the filter doesn't need to close/reopen
  // this stream.
  useEffect(() => {
    const eventSource = new EventSource(allJobsEventsUrl())
    eventSource.addEventListener('job_updated', () => void refreshRef.current())
    return () => eventSource.close()
  }, [])

  const updateJob = useCallback((job: JobStatusResponse) => {
    setJobs((prev) => {
      const index = prev.findIndex((j) => j.id === job.id)
      if (index === -1) return [job, ...prev]
      const next = [...prev]
      next[index] = job
      return next
    })
  }, [])

  return { jobs, refresh, updateJob, status, setStatus }
}
