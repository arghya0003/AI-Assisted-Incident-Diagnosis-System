---
incident_id: incident-0009
title: "orders release requires a configuration variable missing in production"
services: [orders]
fault_type: bad_deploy_errors
source: synthetic
---
**Symptoms:** orders error rate jumped to about 40% immediately after an orders deploy, and front-end checkout showed errors. Latency was normal for requests that succeeded.

**Root cause:** The new version read a configuration variable that existed in staging but had not been added to production, and failed every request on the code path that used it.

**Resolution:** Rolled back orders, added the variable, and redeployed.
