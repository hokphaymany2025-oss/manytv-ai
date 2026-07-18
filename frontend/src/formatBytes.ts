// Small local size-formatting helper, sibling to formatTime.ts -- native
// arithmetic only, no dependency, one concern per file.

const UNITS = ['B', 'KB', 'MB', 'GB'] as const

export function formatBytes(bytes: number | null): string {
  if (bytes == null) return 'unknown size'
  if (bytes === 0) return '0 B'

  let value = bytes
  let unitIndex = 0
  while (value >= 1024 && unitIndex < UNITS.length - 1) {
    value /= 1024
    unitIndex += 1
  }

  const formatted = unitIndex === 0 ? value.toString() : value.toFixed(1)
  return `${formatted} ${UNITS[unitIndex]}`
}
