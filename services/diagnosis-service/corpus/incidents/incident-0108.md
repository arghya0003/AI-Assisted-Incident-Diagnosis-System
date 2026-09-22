---
incident_id: incident-0108
title: "GitHub: peak load repeatedly exhausts ProxySQL connections"
fault_type: db_pool_saturation
source: public_postmortem
source_url: "https://github.blog/2022-03-23-an-update-on-recent-service-disruptions/"
---
**Symptoms:** The shared mysql1 database cluster failed repeatedly over a week.

**Root cause:** Peak-hour load exhausted ProxySQL connections. Memory profiling enabled to debug performance later caused another connection failure.

**Resolution:** Four primary failovers plus another later, an emergency index, and proactive throttling of webhooks and Actions.

**Source:** GitHub blog, https://github.blog/2022-03-23-an-update-on-recent-service-disruptions/ (summary via danluu/post-mortems).
