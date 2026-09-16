---
incident_id: incident-0032
title: "edge-router routing rule typo sends catalogue requests to the wrong backend"
services: [edge-router]
fault_type: config_error
source: synthetic
---
**Symptoms:** Product pages returned 404 errors at the edge and front-end error rate rose, while catalogue received almost no traffic.

**Root cause:** A routing configuration change contained a typo in a path rule, so catalogue requests went to a backend that did not serve them.

**Resolution:** Reverted the routing change.
