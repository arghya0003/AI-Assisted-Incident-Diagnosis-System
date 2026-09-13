---
incident_id: incident-0029
title: "shipping memory alert as the JVM heap grows to its limit"
services: [shipping]
fault_type: benign
source: synthetic
---
**Symptoms:** A memory usage alert fired on shipping some hours after a restart. Latency and error rate were normal, and no user impact was reported.

**Root cause:** None. The JVM expanded its heap towards the configured maximum as designed, and the alert threshold sat below normal steady-state memory.

**Resolution:** No action on the service; the memory alert threshold was retuned.
