---
incident_id: incident-0122
title: "Chef Supermarket: site crashes after launch with very low health-check timeouts"
fault_type: service_crash
source: public_postmortem
source_url: "https://www.chef.io/blog/2014/07/10/supermarket-intermittent-unresponsiveness-postmortem/"
---
**Symptoms:** Intermittent unresponsiveness and increased latency; the site crashed two hours after launch.

**Root cause:** Very low health-check timeouts were one of the main reasons identified.

**Resolution:** Not described in the summary; see the source.

**Source:** Chef blog, https://www.chef.io/blog/2014/07/10/supermarket-intermittent-unresponsiveness-postmortem/ (summary via danluu/post-mortems).
