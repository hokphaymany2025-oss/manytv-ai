import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, getJob, jobEventsUrl } from './api'
import type { JobStatusResponse } from './types'

export function useJob(jobId: string) {
  const [job, setJob] = useState<JobStatusResponse | null>(null)
  const [notFound, setNotFound] = useState(false)
  // A confirmed 404 will never resolve on a later poll (jobs are never
  // deleted) -- a ref (not state) so refresh's own closure can see the
  // latest value synchronously without retriggering the effect below.
  const notFoundRef = useRef(false)

  // Kept as a real, working manual full re-fetch -- no longer polled on a
  // timer, but still useful standalone and preserves this hook's existing
  // public shape. Also doubles as the one-shot existence check the effect
  // below runs before ever opening the SSE connection: EventSource can't
  // expose an HTTP status code to JS once a stream conceptually begins, so
  // the clean ApiError.status === 404 check has to happen via a plain
  // fetch first, exactly as it already did before this migration.
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

    // A ref-like "is this effect still current" flag (plain closure
    // variable, not a React ref -- this effect's own cleanup already scopes
    // it correctly) guards against a stale EventSource being opened after
    // jobId has already changed again: refresh() is awaited before opening
    // the stream, so if the component unmounts or jobId changes again while
    // that initial fetch is still in flight, the continuation below must
    // not open a connection for a job this hook no longer cares about.
    let cancelled = false
    let eventSource: EventSource | null = null

    void (async () => {
      await refresh()
      if (cancelled || notFoundRef.current) return
      eventSource = new EventSource(jobEventsUrl(jobId))
      eventSource.addEventListener('job_updated', (event: MessageEvent<string>) => {
        setJob(JSON.parse(event.data) as JobStatusResponse)
      })
    })()

    return () => {
      cancelled = true
      eventSource?.close()
    }
  }, [jobId, refresh])

  return { job, notFound, refresh, updateJob: setJob }
}
