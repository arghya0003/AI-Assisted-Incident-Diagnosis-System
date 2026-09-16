---
incident_id: incident-0121
title: "Duo: request queue overloads insufficient database capacity"
fault_type: capacity
source: public_postmortem
source_url: "https://status.duo.com/incidents/4w07bmvnt359"
---
**Symptoms:** A cascading failure.

**Root cause:** A request queue overloaded database capacity that was already insufficient. Inadequate capacity planning and monitoring contributed.

**Resolution:** Not described in the summary; see the source.

**Source:** Duo status incident, https://status.duo.com/incidents/4w07bmvnt359 (summary via danluu/post-mortems).
