---
incident_id: incident-0007
title: "shipping release retries queue publishes without backoff"
services: [shipping]
fault_type: bad_deploy_latency
source: synthetic
---
**Symptoms:** After a shipping deploy, shipping p99 latency rose and orders checkout slowed, because orders calls shipping. rabbitmq showed bursts of repeated publishes.

**Root cause:** The release retried failed publishes to rabbitmq immediately in a tight loop, holding request threads while it did so.

**Resolution:** Rolled back shipping; the retry was changed to exponential backoff.
