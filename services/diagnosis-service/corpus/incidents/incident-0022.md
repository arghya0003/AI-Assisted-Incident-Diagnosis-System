---
incident_id: incident-0022
title: "catalogue deploy lowers the connection pool maximum from 50 to 5"
services: [catalogue]
fault_type: db_pool_saturation
source: synthetic
---
**Symptoms:** catalogue p99 latency climbed within minutes of a catalogue deploy as requests queued waiting for database connections, and front-end product pages slowed.

**Root cause:** A configuration typo in the release set the database pool maximum to 5 instead of 50.

**Resolution:** Rolled back the deploy. Pool saturation can be caused by a deploy too, so check config diffs.
