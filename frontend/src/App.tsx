import './App.css'
import { JobList } from './components/JobList'
import { ScriptForm } from './components/ScriptForm'
import { StoryboardForm } from './components/StoryboardForm'
import { useTrackedJobs } from './useTrackedJobs'

function App() {
  const { jobs, trackJob, updateJob } = useTrackedJobs()

  return (
    <div className="app">
      <h1>ManyTV</h1>

      <div className="forms">
        <ScriptForm onSubmitted={trackJob} />
        <StoryboardForm onSubmitted={trackJob} />
      </div>

      <h2>Jobs</h2>
      <JobList jobs={jobs} onJobChanged={updateJob} />
    </div>
  )
}

export default App
