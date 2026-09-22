---
incident_id: incident-0113
title: "incident.io: many small transactions exhaust the database connection pool"
fault_type: db_pool_saturation
source: public_postmortem
source_url: "https://incident.io/blog/database-performance"
---
**Symptoms:** Two weeks of intermittent app timeouts. Traces showed requests waiting up to 20 seconds for a connection from Go's database/sql pool, with the contention spread across many endpoints rather than one slow query.

**Root cause:** An unnecessary transaction wrapped every Slack modal submission. Many small, fast transactions together exhausted the pool.

**Resolution:** Found after many rounds of fixes, by adding middleware that attributed connection-pool hold time to each operation. Pool exhaustion need not have a single slow query behind it.

**Source:** incident.io blog, https://incident.io/blog/database-performance (summary via danluu/post-mortems).
