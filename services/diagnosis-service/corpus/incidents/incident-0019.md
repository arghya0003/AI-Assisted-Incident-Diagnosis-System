---
incident_id: incident-0019
title: "queue-master stops consuming and the shipping queue backs up"
services: [queue-master]
fault_type: service_crash
source: synthetic
---
**Symptoms:** No user-facing errors at first. The shipping queue in rabbitmq grew steadily for an hour until shipping publishes slowed.

**Root cause:** queue-master had crashed and was not restarted, so nothing drained the queue.

**Resolution:** Restarted queue-master; the backlog drained within minutes.
