---
incident_id: incident-0031
title: "p99 latency alert on front-end during near-zero traffic"
services: [front-end]
fault_type: benign
source: synthetic
---
**Symptoms:** A front-end p99 latency alert fired overnight while front-end served only a few requests per minute. No errors and no complaints.

**Root cause:** None. With so few requests, a single slow request determined the p99 value.

**Resolution:** No action; latency alerts now require a minimum request rate before firing.
