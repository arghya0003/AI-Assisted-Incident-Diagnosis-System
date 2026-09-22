import type { Incident } from '../types'

function field(label: string, value: unknown) {
  if (value === null || value === undefined || value === '') return null
  return (
    <div key={label} className="flex justify-between gap-4 py-1 text-sm">
      <dt className="text-slate-500 dark:text-slate-400">{label}</dt>
      <dd className="text-right font-mono text-xs text-slate-800 dark:text-slate-100">
        {Array.isArray(value) ? value.join(', ') : String(value)}
      </dd>
    </div>
  )
}

/** Renders the anomaly snapshot an incident was opened for (CONTRACTS.md's
 * anomalies.detected shape), so an operator can inspect the evidence a hypothesis is built
 * on without leaving the approval console -- "an engineer should be able to click a
 * hypothesis and see the deploy diff ... that justified it" (PLAN.md). */
export function EvidencePanel({ incident }: { incident: Incident }) {
  const anomaly = incident.anomaly as Record<string, unknown>
  const contributors = (anomaly.contributors as Array<Record<string, unknown>> | undefined) ?? []

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-900">
      <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-200">Anomaly evidence</h3>
      <dl className="mt-1 divide-y divide-slate-100 dark:divide-slate-800">
        {field('anomaly_id', incident.anomaly_id)}
        {field('services', incident.services)}
        {field('metrics', anomaly.metrics)}
        {field('detector', anomaly.detector)}
        {field('t_onset', anomaly.t_onset)}
        {field('t_detected', anomaly.t_detected)}
        {field('in_deploy_window', anomaly.in_deploy_window)}
        {field('related_deploy_ids', anomaly.related_deploy_ids)}
      </dl>
      {contributors.length > 0 && (
        <div className="mt-2">
          <h4 className="text-xs font-medium text-slate-500 dark:text-slate-400">Contributing signals</h4>
          <table className="mt-1 w-full text-left text-xs">
            <thead className="text-slate-400">
              <tr>
                <th className="pr-2 font-normal">service</th>
                <th className="pr-2 font-normal">metric</th>
                <th className="pr-2 font-normal">value</th>
                <th className="pr-2 font-normal">baseline</th>
                <th className="font-normal">score</th>
              </tr>
            </thead>
            <tbody className="font-mono text-slate-700 dark:text-slate-300">
              {contributors.map((c, i) => (
                <tr key={i}>
                  <td className="pr-2">{String(c.service)}</td>
                  <td className="pr-2">{String(c.metric)}</td>
                  <td className="pr-2">{c.value !== undefined ? Number(c.value).toFixed(2) : '—'}</td>
                  <td className="pr-2">{c.baseline !== undefined ? Number(c.baseline).toFixed(2) : '—'}</td>
                  <td>{c.score !== undefined ? Number(c.score).toFixed(2) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
