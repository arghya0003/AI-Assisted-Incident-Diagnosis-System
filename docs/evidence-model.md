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
as a migration or recreate the development volume before using the table. A future M3
service should expose/query this table and return `evidence_id` values in `/analyze`
responses.
