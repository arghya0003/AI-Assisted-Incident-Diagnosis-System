---
incident_id: incident-0013
title: "user container stops after its host is drained"
services: [user]
fault_type: service_crash
source: synthetic
---
**Symptoms:** orders and front-end both reported errors, on checkout and on login. user reported no metrics at all during the incident.

**Root cause:** The node running user was drained for maintenance and the user container was not rescheduled, so every call to user failed.

**Resolution:** Restarted user on another node, and both callers recovered at once. The two alerting services shared a single dependency, user.
