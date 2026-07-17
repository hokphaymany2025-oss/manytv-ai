import { useState, type FormEvent } from 'react'
import { submitScript } from '../api'

interface Props {
  onSubmitted: () => void
}

export function ScriptForm({ onSubmitted }: Props) {
  const [prompt, setPrompt] = useState('')
  const [tone, setTone] = useState('neutral')
  const [targetDurationSeconds, setTargetDurationSeconds] = useState(30)
  const [projectId, setProjectId] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await submitScript({
        prompt,
        tone,
        target_duration_seconds: targetDurationSeconds,
        project_id: projectId || undefined,
      })
      onSubmitted()
      setPrompt('')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="job-form">
      <h2>Generate Script</h2>
      <label>
        Prompt
        <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} required rows={3} />
      </label>
      <label>
        Tone
        <input value={tone} onChange={(e) => setTone(e.target.value)} />
      </label>
      <label>
        Target duration (seconds)
        <input
          type="number"
          min={5}
          max={600}
          value={targetDurationSeconds}
          onChange={(e) => setTargetDurationSeconds(Number(e.target.value))}
        />
      </label>
      <label>
        Project ID (optional)
        <input value={projectId} onChange={(e) => setProjectId(e.target.value)} />
      </label>
      <button type="submit" disabled={submitting || !prompt.trim()}>
        {submitting ? 'Submitting…' : 'Generate Script'}
      </button>
      {error && <p className="form-error">{error}</p>}
    </form>
  )
}
