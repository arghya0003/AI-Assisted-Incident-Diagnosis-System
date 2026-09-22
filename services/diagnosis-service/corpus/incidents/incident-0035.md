---
incident_id: incident-0035
title: "front-end release calls the wrong carts API path"
services: [front-end]
fault_type: bad_deploy_errors
source: synthetic
---
**Symptoms:** Basket operations failed and front-end error rate rose right after a front-end deploy. carts logged 404s but was otherwise healthy.

**Root cause:** The release changed the carts endpoint path in front-end configuration to a path carts did not serve.

**Resolution:** Rolled back front-end.
