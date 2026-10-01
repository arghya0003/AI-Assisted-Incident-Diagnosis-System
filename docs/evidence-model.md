# Evidence model

The diagnosis layer can explain a hypothesis using one unified evidence structure:

```text
Evidence
├── Anomaly
├── Metrics
├── Deployment history
├── Service dependencies
└── Similar past incidents
```

Evidence is stored in the `evidence` table in the TimescaleDB `metrics` database. The
`category` column identifies the branch, `source_id` points to the originating record,
and `payload` stores category-specific fields.

| Category | Example `source_id` | Typical payload |
| --- | --- | --- |
| `anomaly` | `anom-0001` | severity, onset, affected metrics |
| `metrics` | `catalogue:latency_p99_ms:2026-08-12T20:45:00.123Z` | value, baseline, labels |
| `deployment` | `dep-2026-08-12-0007` | version, commit, config diff |
| `dependency` | `front-end->catalogue` | relationship, edge type |
| `similar_incident` | `incident-0042` | similarity, resolution, outcome |

The table is initialized automatically for a fresh TimescaleDB volume by
`timescaledb/init/004_evidence.sql`. If the database volume already exists, run that SQL
as a migration or recreate the development volume before using the table.

**Resolved (issue #37): `/analyze` returns source ids, not `evidence_id` values.** An earlier
version of this document said the opposite, which contradicted the example in CONTRACTS.md and left
consumers guessing. A hypothesis cites the records themselves - `anom-…`, `dep-…`, `incident-…` -
because those are meaningful to a human reading the approval console, they are what M4's
`GET /evidence/{id}` resolves, and they survive the replay of a stored analysis.

`diagnosis-service` does write this table on every analysis, one
`ev:<incident_id>:<category>:<source_id>` row per evidence item, carrying the summary and the
payload behind it. That is where the richer record lives, and `GET /candidates/{anomaly_id}` returns
those rows in full. A consumer resolving citations should accept both forms.
