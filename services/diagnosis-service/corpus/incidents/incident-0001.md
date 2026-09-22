---
incident_id: incident-0001
title: "catalogue release adds per-item image lookups and slows product pages"
services: [catalogue]
fault_type: bad_deploy_latency
source: synthetic
---
**Symptoms:** Within five minutes of a catalogue deploy, catalogue p95 latency rose from about 40 ms to over 300 ms and front-end product listing pages slowed with it. Error rate stayed flat. Query volume on catalogue-db tripled.

**Root cause:** The new catalogue version fetched image metadata with one database query per item instead of one per page, multiplying round trips on every request.

**Resolution:** Rolled back the catalogue deploy; latency returned to baseline within two minutes. The query was batched before redeploying.
