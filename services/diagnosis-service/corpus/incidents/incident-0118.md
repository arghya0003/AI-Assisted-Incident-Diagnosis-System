---
incident_id: incident-0118
title: "Stackdriver: Cassandra cluster crash blocks producers and fails the application"
fault_type: service_crash
source: public_postmortem
source_url: "https://www.stackdriver.com/post-mortem-october-23-stackdriver-outage/"
---
**Symptoms:** The entire application failed.

**Root cause:** The Cassandra cluster that ingested data from a message bus crashed. Services publishing to the bus then blocked on queue inserts, and the failure spread to the whole application.

**Resolution:** Not described in the summary; see the source.

**Source:** Stackdriver postmortem, https://www.stackdriver.com/post-mortem-october-23-stackdriver-outage/ (summary via danluu/post-mortems).
