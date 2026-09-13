---
incident_id: incident-0115
title: "incident.io: poison-pill event repeatedly crashes the app"
fault_type: service_crash
source: public_postmortem
source_url: "https://incident.io/blog/intermittent-downtime"
---
**Symptoms:** Intermittent downtime as the app crashed again and again.

**Root cause:** A bad event in the async workers queue triggered unhandled panics each time it was processed.

**Resolution:** Caught corner cases in Go panic recovery and split work by type so one bad class of work could not take everything down.

**Source:** incident.io blog, https://incident.io/blog/intermittent-downtime (summary via danluu/post-mortems).
