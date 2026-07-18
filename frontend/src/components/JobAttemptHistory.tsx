import { formatAbsolute, formatRelative } from '../formatTime'
import { useJobAttempts } from '../useJobAttempts'

interface Props {
  jobId: string
}

export function JobAttemptHistory({ jobId }: Props) {
  const { attempts } = useJobAttempts(jobId)

  if (attempts.length === 0) return <p>No previous attempts.</p>

  return (
    <ul className="job-attempts">
      {attempts.map((attempt, index) => (
        <li
          key={`${attempt.recorded_at}-${index}`}
          className={`job-attempts__entry job-attempts__entry--${attempt.status}`}
        >
          <div className="job-attempts__header">
            <span className="job-attempts__number">Attempt {index + 1}</span>
            <span className="job-attempts__status">{attempt.status}</span>
          </div>
          {attempt.started_at != null && (
            <span className="job-attempts__time">
              Started: {formatAbsolute(attempt.started_at)} ({formatRelative(attempt.started_at)})
            </span>
          )}
          {attempt.finished_at != null && (
            <span className="job-attempts__time">
              Finished: {formatAbsolute(attempt.finished_at)} ({formatRelative(attempt.finished_at)})
            </span>
          )}
          {attempt.error && <p className="job-attempts__error">{attempt.error}</p>}
        </li>
      ))}
    </ul>
  )
}
