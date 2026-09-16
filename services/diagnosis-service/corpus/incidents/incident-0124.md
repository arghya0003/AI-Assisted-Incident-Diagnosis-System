---
incident_id: incident-0124
title: "Buildkite: database downgrade leaves too little capacity at peak"
fault_type: capacity
source: public_postmortem
source_url: "https://building.buildkite.com/outage-post-mortem-for-august-23rd-82b619a3679b"
---
**Symptoms:** Dependent servers collapsed in a cascade at peak.

**Root cause:** Database capacity had been downgraded to reduce AWS spend and could not support customers at peak load.

**Resolution:** Not described in the summary; see the source.

**Source:** Buildkite postmortem, https://building.buildkite.com/outage-post-mortem-for-august-23rd-82b619a3679b (summary via danluu/post-mortems).
