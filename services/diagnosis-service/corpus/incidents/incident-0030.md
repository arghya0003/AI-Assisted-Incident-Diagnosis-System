---
incident_id: incident-0030
title: "carts memory alert from normal garbage collection"
services: [carts]
fault_type: benign
source: synthetic
---
**Symptoms:** carts memory usage alerts fired repeatedly with no change in latency, errors or traffic.

**Root cause:** None. Normal garbage-collection cycles produced a sawtooth memory pattern that crossed a static threshold.

**Resolution:** No action; the alert was changed to fire only on sustained growth.
