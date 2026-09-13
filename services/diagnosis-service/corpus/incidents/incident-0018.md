---
incident_id: incident-0018
title: "rabbitmq blocks publishers after running out of disk"
services: [rabbitmq]
fault_type: service_crash
source: synthetic
---
**Symptoms:** shipping latency and errors rose on queue publishes, and orders checkout slowed. queue-master consumed nothing.

**Root cause:** rabbitmq's disk alarm triggered when its data volume filled, blocking all publishers.

**Resolution:** Freed disk space and restarted rabbitmq, then added disk usage alerts.
