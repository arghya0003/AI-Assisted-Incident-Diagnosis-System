import { useState } from 'react'

const REASON_CATEGORIES = [
  ['wrong_root_cause', 'Wrong root cause'],
  ['wrong_action', 'Right cause, wrong action'],
  ['insufficient_evidence', 'Insufficient evidence'],
  ['duplicate', 'Duplicate of another incident'],
  ['other', 'Other'],
] as const

export function DecisionPanel({
  selectedRank,
  busy,
  onApprove,
  onReject,
  onRequestInfo,
}: {
  selectedRank: number | null
  busy: boolean
  onApprove: (approver: string, note: string) => void
  onReject: (approver: string, reason: string, category: string) => void
  onRequestInfo: (approver: string, note: string) => void
}) {
  const [approver, setApprover] = useState('')
  const [note, setNote] = useState('')
  const [reasonCategory, setReasonCategory] = useState<string>('wrong_root_cause')
  const [mode, setMode] = useState<'approve' | 'reject' | 'info' | null>(null)

  const canSubmit = approver.trim().length > 0 && !busy

  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 dark:border-slate-700 dark:bg-slate-900">
      <label className="block text-xs font-medium text-slate-600 dark:text-slate-300">
        Approver
        <input
          value={approver}
          onChange={(e) => setApprover(e.target.value)}
          placeholder="your name"
          className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-slate-600 dark:bg-slate-800"
        />
      </label>

      {mode === 'reject' && (
        <label className="mt-2 block text-xs font-medium text-slate-600 dark:text-slate-300">
          Reason category
          <select
            value={reasonCategory}
            onChange={(e) => setReasonCategory(e.target.value)}
            className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-slate-600 dark:bg-slate-800"
          >
            {REASON_CATEGORIES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      )}

      {(mode === 'reject' || mode === 'info' || mode === 'approve') && (
        <label className="mt-2 block text-xs font-medium text-slate-600 dark:text-slate-300">
          {mode === 'reject' ? 'Reason (required)' : mode === 'info' ? 'What do you need? (required)' : 'Note (optional)'}
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={2}
            className="mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-slate-600 dark:bg-slate-800"
          />
        </label>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          disabled={!canSubmit || selectedRank === null}
          onClick={() => {
            if (mode !== 'approve') {
              setMode('approve')
              return
            }
            onApprove(approver, note)
          }}
          title={selectedRank === null ? 'Select a hypothesis first' : undefined}
          className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-40"
        >
          {mode === 'approve' ? 'Confirm approve' : 'Approve'}
        </button>
        <button
          disabled={!canSubmit || (mode === 'reject' && note.trim().length === 0)}
          onClick={() => {
            if (mode !== 'reject') {
              setMode('reject')
              return
            }
            onReject(approver, note, reasonCategory)
          }}
          className="rounded bg-rose-600 px-3 py-1.5 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-40"
        >
          {mode === 'reject' ? 'Confirm reject' : 'Reject'}
        </button>
        <button
          disabled={!canSubmit || (mode === 'info' && note.trim().length === 0)}
          onClick={() => {
            if (mode !== 'info') {
              setMode('info')
              return
            }
            onRequestInfo(approver, note)
          }}
          className="rounded border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-600 dark:text-slate-200"
        >
          {mode === 'info' ? 'Send request' : 'Request more info'}
        </button>
      </div>
      {selectedRank === null && <p className="mt-2 text-xs text-slate-500">Select a hypothesis above to approve it.</p>}
    </div>
  )
}
