import './App.css'
import { JobList } from './components/JobList'
import { ScriptForm } from './components/ScriptForm'
import { StoryboardForm } from './components/StoryboardForm'
import { useJobList } from './useJobList'

function App() {
  const { jobs, refresh, updateJob } = useJobList()

  return (
    <div className="app">
      <h1>ManyTV</h1>

      <div className="forms">
        <ScriptForm onSubmitted={refresh} />
        <StoryboardForm onSubmitted={refresh} />
      </div>

      <h2>Jobs</h2>
      <JobList jobs={jobs} onJobChanged={updateJob} />
    </div>
  )
}

export default App
