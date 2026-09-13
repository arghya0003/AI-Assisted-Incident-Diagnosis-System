---
incident_id: incident-0025
title: "retry storm after a network blip exhausts carts-db connections"
services: [carts]
fault_type: db_pool_saturation
source: synthetic
---
**Symptoms:** carts error rate rose after a short network interruption and stayed high after the network recovered.

**Root cause:** carts retried failed database operations immediately, opening new connections faster than old ones closed, until carts-db refused new connections.

**Resolution:** Restarted carts to clear the storm and added backoff to its retries.
