---
incident_id: incident-0012
title: "carts crash-loops on a malformed stored cart"
services: [carts]
fault_type: service_crash
source: synthetic
---
**Symptoms:** Basket requests from front-end failed and front-end error rate rose. carts went missing from monitoring between restarts.

**Root cause:** One malformed cart document in carts-db triggered an unhandled exception while loading, crashing the carts process every time it was read.

**Resolution:** Restarted carts with the bad record quarantined, then added input validation.
