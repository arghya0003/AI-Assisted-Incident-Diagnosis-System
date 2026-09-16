---
incident_id: incident-0017
title: "front-end process exits on an unhandled promise rejection"
services: [front-end]
fault_type: service_crash
source: synthetic
---
**Symptoms:** edge-router returned 502 errors for every page. The backend services were healthy but received almost no traffic.

**Root cause:** A rare error path in front-end left a promise rejection unhandled, which terminated the Node.js process.

**Resolution:** Restarted front-end and added a global rejection handler.
