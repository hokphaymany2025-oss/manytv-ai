import { useCallback, useEffect, useState } from 'react'
import { listJobs } from './api'
import type { JobStatusResponse } from './types'

const POLL_INTERVAL_MS = 3000

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

  useEffect(() => {
    void refresh()
    const interval = setInterval(() => void refresh(), POLL_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [refresh])

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
