import { JobRow } from './JobRow'
import type { JobStatusResponse } from '../types'

interface Props {
  jobs: JobStatusResponse[]
  onJobChanged: (job: JobStatusResponse) => void
  filtered?: boolean
}

export function JobList({ jobs, onJobChanged, filtered = false }: Props) {
  if (jobs.length === 0) {
    return <p>{filtered ? 'No jobs match this filter.' : 'No jobs yet.'}</p>
  }

  return (
    <ul className="job-list">
      {jobs.map((job) => (
        <JobRow key={job.id} job={job} onChanged={onJobChanged} />
      ))}
    </ul>
  )
}
