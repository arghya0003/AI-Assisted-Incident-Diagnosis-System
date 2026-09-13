---
incident_id: incident-0015
title: "shipping crash-loops after losing its queue connection"
services: [shipping]
fault_type: service_crash
source: synthetic
---
**Symptoms:** orders checkout errors rose. shipping restarted repeatedly and reported metrics only intermittently.

**Root cause:** A bug in shipping's reconnect handling crashed the process whenever its rabbitmq connection dropped, and rabbitmq had just been restarted for maintenance.

**Resolution:** Restarted shipping once rabbitmq was stable; the reconnect bug was fixed.
