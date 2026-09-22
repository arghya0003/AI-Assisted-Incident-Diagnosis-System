---
incident_id: incident-0008
title: "catalogue deploy lowers its CPU limit and throttles the service"
services: [catalogue]
fault_type: bad_deploy_latency
source: synthetic
---
**Symptoms:** catalogue p95 and p99 latency spiked under ordinary traffic after a catalogue deploy, while its CPU usage sat flat at a low ceiling. front-end product pages slowed.

**Root cause:** The deployment manifest in the release cut catalogue's CPU limit by 90%, so the container was throttled.

**Resolution:** Rolled back the deploy to restore the previous CPU limit.
