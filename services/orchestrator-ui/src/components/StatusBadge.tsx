import type { IncidentState } from '../types'

const STATE_STYLES: Record<IncidentState, string> = {
  DETECTED: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  ANALYZING: 'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300 animate-pulse',
  ANALYSIS_FAILED: 'bg-orange-100 text-orange-700 dark:bg-orange-900 dark:text-orange-300',
  AWAITING_APPROVAL: 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-300',
  APPROVED: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-300',
  REJECTED: 'bg-rose-100 text-rose-700 dark:bg-rose-900 dark:text-rose-300',
  EXPIRED: 'bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-400',
}

export function StateBadge({ state }: { state: IncidentState }) {
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${STATE_STYLES[state]}`}>
      {state.replace('_', ' ')}
    </span>
  )
}

const SEVERITY_STYLES: Record<string, string> = {
  high: 'bg-rose-500',
  medium: 'bg-amber-500',
  low: 'bg-slate-400',
}

export function SeverityDot({ severity }: { severity: string }) {
  return (
    <span
      className={`inline-block h-2.5 w-2.5 rounded-full ${SEVERITY_STYLES[severity] ?? 'bg-slate-400'}`}
      title={`severity: ${severity}`}
    />
  )
}
