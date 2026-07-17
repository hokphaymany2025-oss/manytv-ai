import { JobRow } from './JobRow'
import type { JobStatusResponse } from '../types'

interface Props {
  jobs: JobStatusResponse[]
  onJobChanged: (job: JobStatusResponse) => void
}

export function JobList({ jobs, onJobChanged }: Props) {
  if (jobs.length === 0) {
    return <p>No jobs submitted from this browser yet.</p>
  }

  return (
    <ul className="job-list">
      {jobs.map((job) => (
        <JobRow key={job.id} job={job} onChanged={onJobChanged} />
      ))}
    </ul>
  )
}
