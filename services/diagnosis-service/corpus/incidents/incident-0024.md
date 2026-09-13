---
incident_id: incident-0024
title: "slow unindexed query on user-db holds connections"
services: [user]
fault_type: db_pool_saturation
source: synthetic
---
**Symptoms:** user p95 latency rose and logins through front-end slowed. user's connection pool was fully in use.

**Root cause:** A new customer lookup query had no index, so each call held a user-db connection for seconds.

**Resolution:** Added the missing index, and pool usage dropped back to normal.
