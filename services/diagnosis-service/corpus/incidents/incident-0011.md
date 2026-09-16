---
incident_id: incident-0011
title: "catalogue killed by out-of-memory; product pages fail"
services: [catalogue]
fault_type: service_crash
source: synthetic
---
**Symptoms:** front-end error rate on product pages rose sharply. catalogue itself stopped reporting metrics for several minutes, so no catalogue alert fired; only its caller front-end alerted.

**Root cause:** An unbounded in-memory image cache grew until the catalogue container was OOM-killed, and it was killed again after each restart.

**Resolution:** Restarted catalogue to stabilise it, then capped the cache size. A service that disappears from metrics can be the cause of its callers' errors.
