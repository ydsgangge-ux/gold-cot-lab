/** Number and date helpers. Kept in one file so every panel formats alike. */

export type RangeKey = '1y' | '3y' | '5y' | 'all'

export const RANGE_LABELS: Array<{ key: RangeKey; label: string }> = [
  { key: '1y', label: '近 1 年' },
  { key: '3y', label: '近 3 年' },
  { key: '5y', label: '近 5 年' },
  { key: 'all', label: '全部可用历史' },
]

/** Start date for a range, anchored at the latest available date. '' = no bound. */
export function shiftYears(anchor: string, range: RangeKey): string {
  if (!anchor || range === 'all') return ''
  const years = Number(range.replace('y', ''))
  const parts = anchor.slice(0, 10).split('-').map(Number)
  if (parts.length !== 3 || parts.some(Number.isNaN)) return ''
  const shifted = new Date(Date.UTC(parts[0] - years, parts[1] - 1, parts[2]))
  return shifted.toISOString().slice(0, 10)
}

/** 1234567 -> "1,234,567"; null stays a dash so "missing" never looks like zero. */
export function num(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

export function signed(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const text = num(Math.abs(value), digits)
  return value > 0 ? `+${text}` : value < 0 ? `-${text}` : text
}

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${num(value, digits)}%`
}

export function ratio(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${num(value * 100, digits)}%`
}

/** "2026-10-09T19:30:00Z" -> "2026-10-09 19:30 UTC" */
export function stampUtc(value: string | null | undefined): string {
  if (!value) return '—'
  const [date, time] = value.replace('Z', '').split('T')
  return time ? `${date} ${time.slice(0, 5)} UTC` : date
}

/** Position of a value inside its trailing window, for the "where are we" bar. */
export function clampPct(value: number | null | undefined): number {
  if (value === null || value === undefined || Number.isNaN(value)) return 0
  return Math.max(0, Math.min(100, value))
}