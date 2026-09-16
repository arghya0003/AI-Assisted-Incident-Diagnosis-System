---
incident_id: incident-0003
title: "front-end release disables template caching and saturates CPU"
services: [front-end]
fault_type: bad_deploy_latency
source: synthetic
---
**Symptoms:** front-end p95 latency doubled and its CPU usage hit its limit shortly after a front-end deploy. The backend services it calls (catalogue, carts, orders, user) all showed normal latency.

**Root cause:** A configuration flag in the release turned off rendered-template caching, so every page was rendered from scratch.

**Resolution:** Rolled back the front-end deploy; CPU and latency recovered immediately. The backends were healthy throughout, so the regression was in front-end itself.
