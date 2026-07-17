const STATUSES = ['queued', 'running', 'resuming', 'cancelling', 'cancelled', 'done', 'failed']

interface Props {
  value: string
  onChange: (value: string) => void
}

export function StatusFilter({ value, onChange }: Props) {
  return (
    <label className="status-filter">
      Filter by status
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">All</option>
        {STATUSES.map((status) => (
          <option key={status} value={status}>
            {status}
          </option>
        ))}
      </select>
    </label>
  )
}
