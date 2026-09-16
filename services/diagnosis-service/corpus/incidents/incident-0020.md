---
incident_id: incident-0020
title: "reporting job exhausts catalogue-db connections"
services: [catalogue]
fault_type: db_pool_saturation
source: synthetic
---
**Symptoms:** catalogue started timing out while acquiring database connections: error rate and p99 latency rose together, and front-end product pages slowed. No deploy had happened.

**Root cause:** An ad-hoc reporting job opened dozens of long-lived connections to catalogue-db and pushed it to its connection limit, leaving none for catalogue.

**Resolution:** Killed the reporting job, and catalogue recovered within a minute. Reporting was moved to a replica.
