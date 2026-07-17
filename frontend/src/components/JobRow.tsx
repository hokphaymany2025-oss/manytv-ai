import { useState } from 'react'
import { Link } from 'react-router-dom'
import { cancelJob, downloadUrl, retryJob } from '../api'
import type { JobStatusResponse } from '../types'

const CANCELLABLE_STATUSES = new Set(['queued', 'running', 'resuming'])
const RETRYABLE_STATUSES = new Set(['failed', 'cancelled'])

interface Props {
  job: JobStatusResponse
  onChanged: (job: JobStatusResponse) => void
}

export function JobRow({ job, onChanged }: Props) {
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  async function handleCancel() {
    setBusy(true)
    setActionError(null)
    try {
      onChanged(await cancelJob(job.id))
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function handleRetry() {
    setBusy(true)
    setActionError(null)
    try {
      onChanged(await retryJob(job.id))
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const script = job.kind === 'generate_script' ? job.result?.script : undefined

  return (
    <li className="job-row">
      <div className="job-row__header">
        <span className="job-row__kind">{job.kind}</span>
        <Link to={`/jobs/${job.id}`} className="job-row__id">
          <code>{job.id}</code>
        </Link>
        <span className={`job-row__status job-row__status--${job.status}`}>{job.status}</span>
        <div className="job-row__actions">
          {CANCELLABLE_STATUSES.has(job.status) && (
            <button type="button" onClick={handleCancel} disabled={busy}>
              Cancel
            </button>
          )}
          {RETRYABLE_STATUSES.has(job.status) && (
            <button type="button" onClick={handleRetry} disabled={busy}>
              Retry
            </button>
          )}
        </div>
      </div>

      {actionError && <p className="form-error">{actionError}</p>}
      {job.error && <p className="job-row__error">{job.error}</p>}

      {typeof script === 'string' && <pre className="job-row__script">{script}</pre>}

      {job.shots && job.shots.length > 0 && (
        <ul className="job-row__shots">
          {job.shots.map((shot) => (
            <li key={shot.shot_index}>
              <span>
                shot {shot.shot_index}: {shot.status}
                {shot.error ? ` — ${shot.error}` : ''}
              </span>
              {shot.files?.map((file) => (
                <a
                  key={file}
                  className="job-row__download"
                  href={downloadUrl(job.id, shot.shot_index, file)}
                  target="_blank"
                  rel="noreferrer"
                >
                  {file.split(/[\\/]/).pop()}
                </a>
              ))}
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}
