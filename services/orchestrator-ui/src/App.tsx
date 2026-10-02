import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from './api'
import { AuditTrail } from './components/AuditTrail'
import { IncidentDetail } from './components/IncidentDetail'
import { IncidentList } from './components/IncidentList'
import { useIncidentFeed } from './hooks/useIncidentFeed'
import type { ActionVocabulary, AuditEntry, FeedMessage, Incident, IncidentState } from './types'

type Tab = 'incidents' | 'audit'

/** The orchestrator's own explanation where it sends one, so a missing migration reads as a
 *  missing migration rather than as "failed to fetch". */
function describeError(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err)
  try {
    const parsed = JSON.parse(message) as { detail?: string }
    if (parsed.detail) return parsed.detail
  } catch {
    // Not JSON - a network failure or an nginx error page. Show it as-is.
  }
  return message || 'the orchestrator could not be reached'
}

export default function App() {
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [filter, setFilter] = useState<IncidentState | 'ALL'>('ALL')
  const [vocabulary, setVocabulary] = useState<ActionVocabulary | null>(null)
  const [tab, setTab] = useState<Tab>('incidents')
  const [globalAudit, setGlobalAudit] = useState<AuditEntry[]>([])
  const [chainStatus, setChainStatus] = useState<boolean | null>(null)
  // Why this exists: the console used to swallow API failures, so an orchestrator with no tables
  // rendered an empty incident list that looked exactly like a quiet system (issue #30). An empty
  // list and a broken backend must not look the same.
  const [apiError, setApiError] = useState<string | null>(null)

  const refreshIncidents = useCallback(() => {
    api
      .listIncidents()
      .then((list) => {
        setIncidents(list)
        setApiError(null)
      })
      .catch((err: unknown) => setApiError(describeError(err)))
  }, [])

  useEffect(() => {
    refreshIncidents()
    api.actions().then(setVocabulary).catch(() => {})
  }, [refreshIncidents])

  useEffect(() => {
    if (tab !== 'audit') return
    api.globalAudit().then(setGlobalAudit).catch(() => {})
    api
      .verifyAudit()
      .then((r) => setChainStatus(r.intact))
      .catch(() => setChainStatus(null))
  }, [tab])

  const onFeedMessage = useCallback((msg: FeedMessage) => {
    if (!msg.incident) return
    setIncidents((prev) => {
      const next = prev.filter((i) => i.incident_id !== msg.incident!.incident_id)
      return [msg.incident!, ...next].sort((a, b) => b.created_at.localeCompare(a.created_at))
    })
  }, [])

  const wsStatus = useIncidentFeed(onFeedMessage)

  const filtered = useMemo(
    () => (filter === 'ALL' ? incidents : incidents.filter((i) => i.state === filter)),
    [incidents, filter],
  )
  const selected = useMemo(() => incidents.find((i) => i.incident_id === selectedId) ?? null, [incidents, selectedId])

  const awaitingCount = incidents.filter((i) => i.state === 'AWAITING_APPROVAL').length

  return (
    <div className="flex h-screen flex-col bg-slate-100 text-slate-900 dark:bg-slate-950 dark:text-slate-50">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-2.5 dark:border-slate-800 dark:bg-slate-900">
        <div className="flex items-center gap-3">
          <h1 className="text-sm font-semibold">Incident Diagnosis — Approval Console</h1>
          {awaitingCount > 0 && (
            <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900 dark:text-amber-300">
              {awaitingCount} awaiting approval
            </span>
          )}
        </div>
        <div className="flex items-center gap-4">
          <nav className="flex gap-1 text-sm">
            <button
              onClick={() => setTab('incidents')}
              className={`rounded px-2 py-1 ${tab === 'incidents' ? 'bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900' : 'text-slate-500'}`}
            >
              Incidents
            </button>
            <button
              onClick={() => setTab('audit')}
              className={`rounded px-2 py-1 ${tab === 'audit' ? 'bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900' : 'text-slate-500'}`}
            >
              Audit log
            </button>
          </nav>
          <span className="flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
            <span
              className={`h-2 w-2 rounded-full ${
                wsStatus === 'open' ? 'bg-emerald-500' : wsStatus === 'connecting' ? 'bg-amber-500' : 'bg-rose-500'
              }`}
            />
            {wsStatus === 'open' ? 'live' : wsStatus === 'connecting' ? 'connecting…' : 'reconnecting…'}
          </span>
        </div>
      </header>

      {apiError && (
        <div
          role="alert"
          className="border-b border-rose-200 bg-rose-50 px-4 py-2 text-xs text-rose-800 dark:border-rose-900 dark:bg-rose-950 dark:text-rose-200"
        >
          <span className="font-semibold">The incident list could not be loaded.</span> {apiError}
        </div>
      )}

      {tab === 'incidents' ? (
        <div className="flex flex-1 overflow-hidden">
          <aside className="w-80 flex-shrink-0 border-r border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
            <IncidentList
              incidents={filtered}
              selectedId={selectedId}
              onSelect={setSelectedId}
              filter={filter}
              onFilterChange={setFilter}
            />
          </aside>
          <main className="flex-1 overflow-hidden">
            {selected ? (
              <IncidentDetail
                key={selected.incident_id}
                incident={selected}
                vocabulary={vocabulary}
                onChanged={(updated) => setIncidents((prev) => prev.map((i) => (i.incident_id === updated.incident_id ? updated : i)))}
              />
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-slate-400">
                Select an incident to review its evidence and hypotheses.
              </div>
            )}
          </main>
        </div>
      ) : (
        <div className="mx-auto w-full max-w-3xl flex-1 overflow-y-auto p-4">
          {chainStatus !== null && (
            <div
              className={`mb-3 rounded border px-3 py-2 text-sm ${
                chainStatus
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-300'
                  : 'border-rose-300 bg-rose-50 text-rose-700 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-300'
              }`}
            >
              {chainStatus ? 'Audit hash chain intact.' : 'Audit hash chain integrity check FAILED.'}
            </div>
          )}
          <AuditTrail entries={globalAudit} title="Global audit log" />
        </div>
      )}
    </div>
  )
}
