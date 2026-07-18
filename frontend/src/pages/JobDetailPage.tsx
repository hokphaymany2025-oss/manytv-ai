import { Link, useParams } from 'react-router-dom'
import { JobArtifacts } from '../components/JobArtifacts'
import { JobAttemptHistory } from '../components/JobAttemptHistory'
import { JobExecutionLogs } from '../components/JobExecutionLogs'
import { JobRow } from '../components/JobRow'
import { JobTimeline } from '../components/JobTimeline'
import { useJob } from '../useJob'

export function JobDetailPage() {
  const { id } = useParams<{ id: string }>()
  const { job, notFound, updateJob } = useJob(id ?? '')

  return (
    <div className="app">
      <p>
        <Link to="/">← Back to dashboard</Link>
      </p>
      <h1>Job Detail</h1>

      {notFound && <p>Job not found.</p>}
      {!notFound && job === null && <p>Loading…</p>}
      {!notFound && job !== null && (
        <>
          <ul className="job-list">
            <JobRow job={job} onChanged={updateJob} showFiles={false} />
          </ul>
          <h2>Timeline</h2>
          <JobTimeline job={job} />
          <h2>Execution Logs</h2>
          <JobExecutionLogs jobId={job.id} />
          <h2>Attempt History</h2>
          <JobAttemptHistory jobId={job.id} />
          <h2>Artifacts</h2>
          <JobArtifacts job={job} />
        </>
      )}
    </div>
  )
}
