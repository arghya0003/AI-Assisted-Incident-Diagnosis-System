import type { Incident, IncidentState } from '../types'
import { SeverityDot, StateBadge } from './StatusBadge'

const FILTERS: (IncidentState | 'ALL')[] = [
  'ALL',
  'AWAITING_APPROVAL',
  'ANALYZING',
  'ANALYSIS_FAILED',
  'DETECTED',
  'APPROVED',
  'REJECTED',
  'EXPIRED',
]

export function IncidentList({
  incidents,
  selectedId,
  onSelect,
  filter,
  onFilterChange,
}: {
  incidents: Incident[]
  selectedId: string | null
  onSelect: (id: string) => void
  filter: IncidentState | 'ALL'
  onFilterChange: (filter: IncidentState | 'ALL') => void
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap gap-1 border-b border-slate-200 p-2 dark:border-slate-700">
        {FILTERS.map((f) => (
          <button
            key={f}
            onClick={() => onFilterChange(f)}
            className={`rounded px-2 py-1 text-xs font-medium ${
              filter === f
                ? 'bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900'
                : 'text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800'
            }`}
          >
            {f === 'ALL' ? 'All' : f.replace('_', ' ')}
          </button>
        ))}
      </div>
      <ul className="flex-1 overflow-y-auto">
        {incidents.length === 0 && (
          <li className="p-4 text-sm text-slate-500 dark:text-slate-400">No incidents in this view.</li>
        )}
        {incidents.map((incident) => (
          <li key={incident.incident_id}>
            <button
              onClick={() => onSelect(incident.incident_id)}
              className={`block w-full border-b border-slate-100 p-3 text-left transition dark:border-slate-800 ${
                selectedId === incident.incident_id
                  ? 'bg-indigo-50 dark:bg-indigo-950'
                  : 'hover:bg-slate-50 dark:hover:bg-slate-900'
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="flex items-center gap-1.5 truncate font-mono text-xs text-slate-500 dark:text-slate-400">
                  <SeverityDot severity={incident.severity} />
                  {incident.incident_id}
                </span>
                <StateBadge state={incident.state} />
              </div>
              <div className="mt-1 truncate text-sm font-medium text-slate-800 dark:text-slate-100">
                {incident.services.join(', ')}
              </div>
              <div className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
                {new Date(incident.created_at).toLocaleString()}
              </div>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
