---
incident_id: incident-0107
title: "CircleCI: primary database saturates with queued operations at peak"
fault_type: db_contention
source: public_postmortem
source_url: "https://status.circleci.com/incidents/8rklh3qqckp1"
---
**Symptoms:** At peak load the primary database backed up with queued operations and stopped catching up. Builds piled up into a 17-hour backlog.

**Root cause:** Queue depth saturated the primary. Rolling back recent changes had no isolated effect, and the build scheduler's throttles backed off exactly when more throughput was needed.

**Resolution:** A primary failover bought temporary headroom; tooling was rebuilt on the fly to drain the backlog.

**Source:** CircleCI incident report, https://status.circleci.com/incidents/8rklh3qqckp1 (summary via danluu/post-mortems).
