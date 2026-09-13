---
incident_id: incident-0028
title: "nightly export saturates orders CPU"
services: [orders]
fault_type: capacity
source: synthetic
---
**Symptoms:** orders latency spiked every night at about the same time, and checkout was slow for a few minutes.

**Root cause:** A scheduled order export ran inside the orders service and competed with live traffic for CPU.

**Resolution:** Moved the export to a separate worker and scheduled it off-peak.
