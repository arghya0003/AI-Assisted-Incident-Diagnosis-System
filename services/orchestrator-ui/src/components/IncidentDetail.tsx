import { useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import type { ActionVocabulary, AuditEntry, Incident } from '../types'
import { AuditTrail } from './AuditTrail'
import { DecisionPanel } from './DecisionPanel'
import { EvidenceDetail } from './EvidenceDetail'
import { EvidencePanel } from './EvidencePanel'
import { HypothesisCard } from './HypothesisCard'
import { StateBadge } from './StatusBadge'

export function IncidentDetail({
  incident,
  vocabulary,
  onChanged,
}: {
  incident: Incident
  vocabulary: ActionVocabulary | null
  onChanged: (incident: Incident) => void
}) {
  const [selectedRank, setSelectedRank] = useState<number | null>(null)
  const [inspectedEvidenceId, setInspectedEvidenceId] = useState<string | null>(null)
  const [audit, setAudit] = useState<AuditEntry[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Re-fetches whenever this incident changes, including a decision made on it (updated_at
  // moves). Local UI state (selectedRank, error) does not need resetting here: App.tsx keys
  // this component by incident_id, so switching to a different incident remounts it fresh.
  useEffect(() => {
    api
      .incidentAudit(incident.incident_id)
      .then(setAudit)
      .catch(() => setAudit([]))
  }, [incident.incident_id, incident.updated_at])

  async function run<T>(action: () => Promise<T>) {
    setBusy(true)
    setError(null)
    try {
      const result = await action()
      return result
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Request failed')
      return null
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-4">
      <div>
        <div className="flex items-center gap-2">
          <h2 className="font-mono text-base font-semibold text-slate-900 dark:text-slate-50">{incident.incident_id}</h2>
          <StateBadge state={incident.state} />
        </div>
        <p className="text-xs text-slate-500 dark:text-slate-400">
          opened {new Date(incident.created_at).toLocaleString()} · anomaly {incident.anomaly_id}
        </p>
      </div>

      {error && (
        <div className="rounded border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-300">
          {error}
        </div>
      )}

      <EvidencePanel incident={incident} />

      {incident.state === 'ANALYZING' && (
        <div className="rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm text-blue-700 dark:border-blue-800 dark:bg-blue-950 dark:text-blue-300">
          Waiting on diagnosis-service for a ranked hypothesis…
        </div>
      )}

      {incident.state === 'ANALYSIS_FAILED' && (
        <div className="rounded-lg border border-orange-200 bg-orange-50 p-3 text-sm text-orange-800 dark:border-orange-800 dark:bg-orange-950 dark:text-orange-300">
          <p>
            Analysis failed after {incident.analysis_attempts} attempt{incident.analysis_attempts === 1 ? '' : 's'}:{' '}
            <span className="font-mono text-xs">{incident.fail_reason}</span>
          </p>
          <button
            disabled={busy}
            onClick={() => run(() => api.reanalyze(incident.incident_id)).then((r) => r && onChanged(r))}
            className="mt-2 rounded bg-orange-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40"
          >
            Retry analysis
          </button>
        </div>
      )}

      {incident.hypotheses.length > 0 && (
        <div>
          <h3 className="mb-1.5 text-sm font-semibold text-slate-700 dark:text-slate-200">
            Ranked hypotheses
            {incident.model_version && (
              <span className="ml-2 font-normal text-xs text-slate-400">
                {incident.answered_by} · {incident.model_version}
              </span>
            )}
          </h3>
          <div className="space-y-2">
            {incident.hypotheses.map((h) => (
              <HypothesisCard
                key={h.rank}
                hypothesis={h}
                vocabulary={vocabulary}
                selected={selectedRank === h.rank}
                onSelect={() => setSelectedRank(h.rank)}
                decidedRank={incident.decided_hypothesis_rank}
                onInspectEvidence={(id) => setInspectedEvidenceId((current) => (current === id ? null : id))}
                inspectedEvidenceId={inspectedEvidenceId}
              />
            ))}
          </div>
          {inspectedEvidenceId && (
            <div className="mt-2">
              <EvidenceDetail
                key={inspectedEvidenceId}
                evidenceId={inspectedEvidenceId}
                onClose={() => setInspectedEvidenceId(null)}
              />
            </div>
          )}
        </div>
      )}

      {incident.state === 'AWAITING_APPROVAL' && (
        <DecisionPanel
          selectedRank={selectedRank}
          busy={busy}
          onApprove={(approver, note) =>
            run(() => api.approve(incident.incident_id, selectedRank!, approver, note)).then((r) => r && onChanged(r))
          }
          onReject={(approver, reason, category) =>
            run(() => api.reject(incident.incident_id, approver, reason, category, selectedRank ?? undefined)).then(
              (r) => r && onChanged(r),
            )
          }
          onRequestInfo={(approver, note) =>
            run(() => api.requestInfo(incident.incident_id, approver, note)).then((r) => r && onChanged(r))
          }
        />
      )}

      {(incident.state === 'APPROVED' || incident.state === 'REJECTED') && (
        <div
          className={`rounded-lg border p-3 text-sm ${
            incident.state === 'APPROVED'
              ? 'border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-300'
              : 'border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-300'
          }`}
        >
          <p>
            {incident.decision === 'approved' ? 'Approved' : 'Rejected'} by{' '}
            <span className="font-mono">{incident.decided_by}</span> at{' '}
            {incident.decided_at && new Date(incident.decided_at).toLocaleString()}
          </p>
          {incident.decision_reason && <p className="mt-1">"{incident.decision_reason}"</p>}
          {incident.state === 'APPROVED' && (
            <p className="mt-1 text-xs">
              {incident.execution_logged
                ? 'Remediation intent logged to the audit trail. The stubbed executor never acted on the running system.'
                : 'Execution intent not yet logged.'}
            </p>
          )}
        </div>
      )}

      {incident.state === 'EXPIRED' && (
        <div className="rounded-lg border border-gray-300 bg-gray-50 p-3 text-sm text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300">
          No decision was made before the approval window closed. No action was taken.
        </div>
      )}

      <AuditTrail entries={audit} title="Incident audit trail" />
    </div>
  )
}
