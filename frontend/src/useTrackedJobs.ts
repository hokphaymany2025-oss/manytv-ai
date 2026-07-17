import { useCallback, useEffect, useState } from 'react'
import { getJob } from './api'
import type { JobStatusResponse } from './types'

// No list-jobs backend endpoint exists (deliberately out of scope -- see
// TODO.md) -- this browser remembers the job ids it has submitted and
// polls each individually via GET /api/jobs/{id}. The list only ever shows
// jobs submitted from this browser, not full server-side history.
const STORAGE_KEY = 'manytv.trackedJobIds'
const POLL_INTERVAL_MS = 3000

function loadTrackedIds(): string[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? (JSON.parse(raw) as string[]) : []
  } catch {
    return []
  }
}

function saveTrackedIds(ids: string[]): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(ids))
}

export function useTrackedJobs() {
  const [jobsById, setJobsById] = useState<Record<string, JobStatusResponse>>({})
  const [trackedIds, setTrackedIds] = useState<string[]>(loadTrackedIds)

  const trackJob = useCallback((jobId: string) => {
    setTrackedIds((prev) => {
      if (prev.includes(jobId)) return prev
      const next = [jobId, ...prev]
      saveTrackedIds(next)
      return next
    })
  }, [])

  const updateJob = useCallback((job: JobStatusResponse) => {
    setJobsById((prev) => ({ ...prev, [job.id]: job }))
  }, [])

  const refreshJob = useCallback(
    async (jobId: string) => {
      try {
        updateJob(await getJob(jobId))
      } catch {
        // Backend unreachable or job vanished (e.g. a fresh jobs.db) --
        // leave the last-known state in place rather than dropping the row.
      }
    },
    [updateJob],
  )

  useEffect(() => {
    if (trackedIds.length === 0) return

    trackedIds.forEach((id) => void refreshJob(id))
    const interval = setInterval(() => {
      trackedIds.forEach((id) => void refreshJob(id))
    }, POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [trackedIds, refreshJob])

  const jobs = trackedIds
    .map((id) => jobsById[id])
    .filter((job): job is JobStatusResponse => job !== undefined)

  return { jobs, trackJob, updateJob }
}
