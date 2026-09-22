// Talks to the orchestrator through /api, which nginx.conf (production) and vite.config.ts's
// dev server proxy (`npm run dev`) both map onto the orchestrator container/process directly
// -- same-origin from the browser's point of view either way, so no CORS handling is needed
// here.
import type { ActionVocabulary, AuditEntry, Incident, ResolvedEvidence } from './types'

const BASE = '/api'

class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    const body = await response.text()
    throw new ApiError(response.status, body || response.statusText)
  }
  return response.json() as Promise<T>
}

export const api = {
  listIncidents: (state?: string) =>
    request<Incident[]>(`/incidents${state ? `?state=${encodeURIComponent(state)}` : ''}`),
  getIncident: (id: string) => request<Incident>(`/incidents/${id}`),
  incidentAudit: (id: string) => request<AuditEntry[]>(`/incidents/${id}/audit`),
  globalAudit: (limit = 200) => request<AuditEntry[]>(`/audit?limit=${limit}`),
  verifyAudit: () => request<{ intact: boolean; first_broken_audit_id: number | null }>('/audit/verify'),
  actions: () => request<ActionVocabulary>('/actions'),
  // Evidence ids contain colons (and, for metrics, an ISO timestamp), so they must be encoded
  // rather than interpolated raw.
  resolveEvidence: (evidenceId: string) => request<ResolvedEvidence>(`/evidence/${encodeURIComponent(evidenceId)}`),
  approve: (id: string, hypothesis_rank: number, approver: string, note?: string) =>
    request<Incident>(`/incidents/${id}/approve`, {
      method: 'POST',
      body: JSON.stringify({ hypothesis_rank, approver, note: note || null }),
    }),
  reject: (id: string, approver: string, reason: string, reason_category: string, hypothesis_rank?: number) =>
    request<Incident>(`/incidents/${id}/reject`, {
      method: 'POST',
      body: JSON.stringify({ approver, reason, reason_category, hypothesis_rank: hypothesis_rank ?? null }),
    }),
  requestInfo: (id: string, approver: string, note: string) =>
    request<Incident>(`/incidents/${id}/request-info`, {
      method: 'POST',
      body: JSON.stringify({ approver, note }),
    }),
  reanalyze: (id: string) => request<Incident>(`/incidents/${id}/reanalyze`, { method: 'POST' }),
}

export { ApiError }

export function wsUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws`
}
