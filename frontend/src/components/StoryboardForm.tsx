import { useState, type FormEvent } from 'react'
import { submitStoryboard } from '../api'

interface Props {
  onSubmitted: () => void
}

export function StoryboardForm({ onSubmitted }: Props) {
  const [script, setScript] = useState('')
  const [workflowName, setWorkflowName] = useState('default_t2v')
  const [projectId, setProjectId] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await submitStoryboard({
        script,
        workflow_name: workflowName,
        project_id: projectId || undefined,
      })
      onSubmitted()
      setScript('')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="job-form">
      <h2>Storyboard + Generate Video</h2>
      <label>
        Script (one shot per line)
        <textarea value={script} onChange={(e) => setScript(e.target.value)} required rows={5} />
      </label>
      <label>
        Workflow name
        <input value={workflowName} onChange={(e) => setWorkflowName(e.target.value)} />
      </label>
      <label>
        Project ID (optional)
        <input value={projectId} onChange={(e) => setProjectId(e.target.value)} />
      </label>
      <button type="submit" disabled={submitting || !script.trim()}>
        {submitting ? 'Submitting…' : 'Generate Storyboard'}
      </button>
      {error && <p className="form-error">{error}</p>}
    </form>
  )
}
