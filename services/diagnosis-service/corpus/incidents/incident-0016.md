---
incident_id: incident-0016
title: "orders exits on startup after a node restart"
services: [orders]
fault_type: service_crash
source: synthetic
---
**Symptoms:** Checkout and order history pages in front-end failed. orders produced no metrics, and payment and shipping, which orders calls, went idle.

**Root cause:** orders ran out of file descriptors on a misconfigured node during startup and exited.

**Resolution:** Restarted orders on a correctly configured node.
