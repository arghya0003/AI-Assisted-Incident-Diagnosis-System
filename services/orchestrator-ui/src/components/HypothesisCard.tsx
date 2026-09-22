import type { ActionVocabulary, Hypothesis } from '../types'

export function HypothesisCard({
  hypothesis,
  vocabulary,
  selected,
  onSelect,
  decidedRank,
}: {
  hypothesis: Hypothesis
  vocabulary: ActionVocabulary | null
  selected: boolean
  onSelect: () => void
  decidedRank: number | null
}) {
  const verb = hypothesis.proposed_action.split(':')[0]
  const target = hypothesis.proposed_action.includes(':') ? hypothesis.proposed_action.split(':')[1] : null
  const blastRadius = vocabulary?.blast_radius[verb] ?? 'unknown'
  const isDecided = decidedRank !== null
  const wasChosen = decidedRank === hypothesis.rank

  return (
    <button
      type="button"
      onClick={isDecided ? undefined : onSelect}
      disabled={isDecided}
      className={`w-full rounded-lg border p-3 text-left transition ${
        selected
          ? 'border-indigo-400 bg-indigo-50 dark:border-indigo-500 dark:bg-indigo-950'
          : wasChosen
            ? 'border-emerald-400 bg-emerald-50 dark:border-emerald-500 dark:bg-emerald-950'
            : 'border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900'
      } ${isDecided && !wasChosen ? 'opacity-50' : ''}`}
    >
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-slate-500 dark:text-slate-400">Rank {hypothesis.rank}</span>
        <span className="text-xs text-slate-500 dark:text-slate-400">
          confidence {(hypothesis.confidence * 100).toFixed(0)}%
        </span>
      </div>
      <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
        <div className="h-full bg-indigo-500" style={{ width: `${hypothesis.confidence * 100}%` }} />
      </div>
      <p className="mt-2 text-sm text-slate-800 dark:text-slate-100">{hypothesis.cause}</p>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <span className="rounded bg-slate-900 px-1.5 py-0.5 font-mono text-xs text-white dark:bg-slate-100 dark:text-slate-900">
          {hypothesis.proposed_action}
        </span>
        <span className="rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-xs text-amber-800 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-300">
          blast radius: {blastRadius}
        </span>
        {target && <span className="text-xs text-slate-400">target: {target}</span>}
      </div>
      <div className="mt-2 flex flex-wrap gap-1">
        {hypothesis.evidence_ids.map((id) => (
          <span
            key={id}
            className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-600 dark:bg-slate-800 dark:text-slate-300"
          >
            {id}
          </span>
        ))}
      </div>
    </button>
  )
}
