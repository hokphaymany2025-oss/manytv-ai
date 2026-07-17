import { JobList } from '../components/JobList'
import { ScriptForm } from '../components/ScriptForm'
import { StatusFilter } from '../components/StatusFilter'
import { StoryboardForm } from '../components/StoryboardForm'
import { useJobList } from '../useJobList'

export function DashboardPage() {
  const { jobs, refresh, updateJob, status, setStatus } = useJobList()

  return (
    <div className="app">
      <h1>ManyTV</h1>

      <div className="forms">
        <ScriptForm onSubmitted={refresh} />
        <StoryboardForm onSubmitted={refresh} />
      </div>

      <div className="jobs-header">
        <h2>Jobs</h2>
        <StatusFilter value={status} onChange={setStatus} />
      </div>
      <JobList jobs={jobs} onJobChanged={updateJob} filtered={status !== ''} />
    </div>
  )
}
