import { useEffect, useState } from 'react'
import { api } from '../api'
import type { ResolvedEvidence } from '../types'

/** Renders whatever one cited evidence id turns out to be. This is what makes a hypothesis's
 * evidence *inspectable* (PLAN.md): an approver clicks a cited id and sees the deploy diff, or
 * the past postmortem, that justified the proposed action — rather than an opaque id string. */
export function EvidenceDetail({ evidenceId, onClose }: { evidenceId: string; onClose: () => void }) {
  const [evidence, setEvidence] = useState<ResolvedEvidence | null>(null)
  const [error, setError] = useState(false)

  // No state reset here: IncidentDetail keys this component by evidenceId, so selecting a
  // different id remounts it with fresh state rather than reusing this one's.
  useEffect(() => {
    api
      .resolveEvidence(evidenceId)
      .then(setEvidence)
      .catch(() => setError(true))
  }, [evidenceId])

  return (
    <div className="rounded-lg border border-indigo-300 bg-indigo-50/60 p-3 dark:border-indigo-700 dark:bg-indigo-950/40">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h4 className="text-sm font-semibold text-slate-800 dark:text-slate-100">Evidence</h4>
          <p className="font-mono text-[11px] break-all text-slate-500 dark:text-slate-400">{evidenceId}</p>
        </div>
        <button
          onClick={onClose}
          className="rounded px-2 py-0.5 text-xs text-slate-500 hover:bg-slate-200 dark:text-slate-400 dark:hover:bg-slate-700"
        >
          Close
        </button>
      </div>

      {error && <p className="mt-2 text-sm text-rose-600 dark:text-rose-400">Could not load this evidence.</p>}
      {!evidence && !error && <p className="mt-2 text-sm text-slate-500">Loading…</p>}

      {evidence && (
        <div className="mt-2">
          <p className="text-sm text-slate-700 dark:text-slate-200">{evidence.summary}</p>

          {evidence.kind === 'deploy' && evidence.deploy && (
            <div className="mt-2 space-y-1 text-xs">
              <div className="flex gap-3 font-mono text-slate-600 dark:text-slate-300">
                <span>{evidence.deploy.service}</span>
                <span>{evidence.deploy.version}</span>
                <span>{evidence.deploy.commit_sha}</span>
              </div>
              <div className="text-slate-500 dark:text-slate-400">
                {new Date(evidence.deploy.time).toLocaleString()}
              </div>
              <div>
                <div className="mt-1 mb-0.5 font-medium text-slate-600 dark:text-slate-300">Config diff</div>
                {evidence.deploy.config_diff ? (
                  <pre className="overflow-x-auto rounded bg-slate-900 p-2 font-mono text-[11px] leading-relaxed text-slate-100">
                    {evidence.deploy.config_diff}
                  </pre>
                ) : (
                  <p className="text-slate-500">No config diff was recorded for this deploy.</p>
                )}
              </div>
            </div>
          )}

          {evidence.kind === 'past_incident' && evidence.past_incident && (
            <div className="mt-2 text-xs">
              <div className="flex flex-wrap gap-1.5">
                {evidence.past_incident.services.map((s) => (
                  <span key={s} className="rounded bg-slate-200 px-1.5 py-0.5 dark:bg-slate-700">
                    {s}
                  </span>
                ))}
                {evidence.past_incident.fault_type && (
                  <span className="rounded bg-slate-200 px-1.5 py-0.5 dark:bg-slate-700">
                    {evidence.past_incident.fault_type}
                  </span>
                )}
              </div>
              <pre className="mt-2 max-h-64 overflow-y-auto rounded bg-white p-2 font-sans text-[11px] leading-relaxed whitespace-pre-wrap text-slate-700 dark:bg-slate-900 dark:text-slate-200">
                {evidence.past_incident.body}
              </pre>
            </div>
          )}

          {evidence.kind === 'anomaly' && evidence.anomaly && (
            <pre className="mt-2 max-h-64 overflow-auto rounded bg-white p-2 font-mono text-[11px] text-slate-700 dark:bg-slate-900 dark:text-slate-200">
              {JSON.stringify(evidence.anomaly, null, 2)}
            </pre>
          )}

          {evidence.kind === 'unknown' && (
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
              The record may have aged out of retention since the analysis ran.
            </p>
          )}
        </div>
      )}
    </div>
  )
}
