---
incident_id: incident-0004
title: "payment library upgrade makes card authorisation CPU-heavy"
services: [payment]
fault_type: bad_deploy_latency
source: synthetic
---
**Symptoms:** payment p99 latency rose from about 20 ms to 900 ms after a payment deploy. orders checkout latency followed, because orders waits on payment. No errors.

**Root cause:** An upgraded cryptography library defaulted to a much more expensive key-derivation setting on every authorisation.

**Resolution:** Rolled back payment, then redeployed with the previous setting pinned explicitly.
