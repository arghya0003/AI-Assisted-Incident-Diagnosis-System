// Mirrors services/orchestrator/app/models.py. Kept as a hand-written copy (not generated)
// so a shape drift on the backend fails a TypeScript build here rather than silently
// rendering `undefined` -- the same reasoning diagnosis-service's Pydantic models use for
// re-declaring M2's AnomalyEvent instead of trusting the JSON blindly.

export type IncidentState =
  | 'DETECTED'
  | 'ANALYZING'
  | 'ANALYSIS_FAILED'
  | 'AWAITING_APPROVAL'
  | 'APPROVED'
  | 'REJECTED'
  | 'EXPIRED'

export interface Hypothesis {
  rank: number
  cause: string
  confidence: number
  evidence_ids: string[]
  proposed_action: string
}

export interface Incident {
  incident_id: string
  anomaly_id: string
  state: IncidentState
  services: string[]
  severity: string
  anomaly: Record<string, unknown>
  analysis_id: string | null
  model_version: string | null
  answered_by: string | null
  hypotheses: Hypothesis[]
  analysis_attempts: number
  fail_reason: string | null
  decision: 'approved' | 'rejected' | null
  decided_hypothesis_rank: number | null
  decided_by: string | null
  decided_at: string | null
  decision_reason: string | null
  execution_logged: boolean
  created_at: string
  updated_at: string
  awaiting_since: string | null
  expires_at: string | null
}

export interface AuditEntry {
  audit_id: number
  incident_id: string | null
  event_type: string
  actor: string
  detail: Record<string, unknown>
  prev_hash: string
  hash: string
  created_at: string
}

export interface ActionVocabulary {
  actions_with_target: string[]
  no_action: string
  blast_radius: Record<string, string>
}

export interface FeedMessage {
  event: string
  incident: Incident | null
}
