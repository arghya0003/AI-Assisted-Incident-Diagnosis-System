---
incident_id: incident-0002
title: "orders release makes a synchronous shipping call during checkout"
services: [orders]
fault_type: bad_deploy_latency
source: synthetic
---
**Symptoms:** After an orders deploy, orders p99 latency climbed to several seconds and checkout through front-end became slow. shipping latency rose slightly; payment and user were normal.

**Root cause:** The release added a blocking call from orders to shipping for delivery estimates, with a 2-second timeout, putting shipping's response time on every checkout request.

**Resolution:** Rolled back orders. The delivery estimate was later made asynchronous.
