import { formatAbsolute, formatRelative } from '../formatTime'
import { useJobLogs } from '../useJobLogs'

interface Props {
  jobId: string
}

export function JobExecutionLogs({ jobId }: Props) {
  const { logs } = useJobLogs(jobId)

  if (logs.length === 0) return <p>No log entries yet.</p>

  return (
    <ul className="job-logs">
      {logs.map((log, index) => (
        <li
          key={`${log.timestamp}-${index}`}
          className={`job-logs__entry job-logs__entry--${log.level}${
            log.shot_index != null ? ' job-logs__entry--shot' : ''
          }`}
        >
          <span className="job-logs__time">
            {formatAbsolute(log.timestamp)} ({formatRelative(log.timestamp)})
          </span>
          {log.shot_index != null && <span className="job-logs__shot">shot {log.shot_index}</span>}
          <span className="job-logs__level">{log.level}</span>
          <span className="job-logs__message">{log.message}</span>
        </li>
      ))}
    </ul>
  )
}
