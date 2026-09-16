---
incident_id: incident-0027
title: "marketing campaign doubles basket traffic"
services: [carts]
fault_type: capacity
source: synthetic
---
**Symptoms:** carts and front-end latency rose steadily during a campaign while error rate stayed low. There was no deploy and no fault.

**Root cause:** Traffic exceeded carts' provisioned capacity.

**Resolution:** Scaled carts horizontally, and latency returned to baseline.
