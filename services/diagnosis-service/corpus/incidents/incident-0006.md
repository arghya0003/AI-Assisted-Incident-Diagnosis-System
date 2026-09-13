---
incident_id: incident-0006
title: "carts release enables debug logging on the hot path"
services: [carts]
fault_type: bad_deploy_latency
source: synthetic
---
**Symptoms:** carts latency and CPU usage rose together minutes after a carts deploy, and basket operations in front-end slowed. carts-db was idle.

**Root cause:** Debug-level logging was left enabled in the release, serialising full cart contents on every request.

**Resolution:** Rolled back carts and fixed the logging level in configuration.
