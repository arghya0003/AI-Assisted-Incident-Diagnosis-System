---
incident_id: incident-0026
title: "schema migration locks the catalogue items table"
services: [catalogue-db]
fault_type: db_contention
source: synthetic
---
**Symptoms:** catalogue queries blocked and latency spiked, then errors followed as requests timed out. front-end product pages hung.

**Root cause:** An online schema migration took a table lock on the catalogue items table in catalogue-db during business hours.

**Resolution:** Paused the migration and reran it off-peak with a non-locking method.
