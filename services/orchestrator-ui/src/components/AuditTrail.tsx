import type { AuditEntry } from '../types'

export function AuditTrail({ entries, title = 'Audit trail' }: { entries: AuditEntry[]; title?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-900">
      <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-200">{title}</h3>
      {entries.length === 0 ? (
        <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">No entries yet.</p>
      ) : (
        <ol className="mt-2 space-y-2">
          {entries.map((entry) => (
            <li key={entry.audit_id} className="border-l-2 border-slate-200 pl-2 text-xs dark:border-slate-700">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-slate-700 dark:text-slate-200">{entry.event_type}</span>
                <span className="text-slate-400">{new Date(entry.created_at).toLocaleString()}</span>
              </div>
              <div className="text-slate-500 dark:text-slate-400">
                by <span className="font-mono">{entry.actor}</span>
                {entry.incident_id && (
                  <>
                    {' · '}
                    <span className="font-mono">{entry.incident_id}</span>
                  </>
                )}
              </div>
              {Object.keys(entry.detail).length > 0 && (
                <pre className="mt-1 overflow-x-auto rounded bg-slate-50 p-1 font-mono text-[11px] text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                  {JSON.stringify(entry.detail, null, 0)}
                </pre>
              )}
              <div className="mt-0.5 truncate font-mono text-[10px] text-slate-300 dark:text-slate-600" title={entry.hash}>
                hash {entry.hash.slice(0, 16)}…
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}
