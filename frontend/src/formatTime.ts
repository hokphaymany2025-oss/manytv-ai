// Small local time-formatting helpers -- native Date/Intl only, no date
// library dependency, matching this frontend's existing zero-extra-dependency
// style.

export function formatAbsolute(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleString()
}

export function formatRelative(unixSeconds: number, now: number = Date.now() / 1000): string {
  const diffSeconds = Math.max(0, Math.round(now - unixSeconds))

  if (diffSeconds < 60) return `${diffSeconds}s ago`

  const diffMinutes = Math.round(diffSeconds / 60)
  if (diffMinutes < 60) return `${diffMinutes}m ago`

  const diffHours = Math.round(diffMinutes / 60)
  if (diffHours < 24) return `${diffHours}h ago`

  const diffDays = Math.round(diffHours / 24)
  return `${diffDays}d ago`
}
