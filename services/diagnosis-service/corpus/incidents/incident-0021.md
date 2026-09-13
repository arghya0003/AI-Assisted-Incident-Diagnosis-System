---
incident_id: incident-0021
title: "catalogue leaks database connections on an error path"
services: [catalogue]
fault_type: db_pool_saturation
source: synthetic
---
**Symptoms:** catalogue latency rose slowly over several hours, then errors appeared once its connection pool ran dry. Restarting catalogue fixed it for a while.

**Root cause:** Connections were not returned to the pool when a particular query failed, so each failure leaked one connection until the pool was exhausted.

**Resolution:** Restarted catalogue to release the connections, then fixed the leak.
