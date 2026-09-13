---
incident_id: incident-0023
title: "orders exhausts its orders-db connection pool during a sale"
services: [orders]
fault_type: db_pool_saturation
source: synthetic
---
**Symptoms:** During a traffic peak, orders latency rose and some checkouts timed out; front-end checkout slowed. orders-db itself had spare capacity.

**Root cause:** orders' client-side connection pool was sized for normal traffic, and its wait queue filled.

**Resolution:** Scaled orders out and raised the pool size.
