---
incident_id: incident-0034
title: "catalogue-db restarts and catalogue errors during crash recovery"
services: [catalogue-db]
fault_type: service_crash
source: synthetic
---
**Symptoms:** catalogue error rate spiked for about two minutes and front-end product pages failed briefly. catalogue-db was unavailable during that window.

**Root cause:** The MySQL process in catalogue-db crashed and ran crash recovery on restart; catalogue could not connect until recovery finished.

**Resolution:** Recovered without intervention. The crash was traced to a memory limit set too low.
