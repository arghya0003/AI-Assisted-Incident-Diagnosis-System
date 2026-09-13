---
incident_id: incident-0106
title: "CircleCI: database upgrade leaves stale statistics and queries hit disk"
fault_type: db_contention
source: public_postmortem
source_url: "https://discuss.circleci.com/t/post-incident-report-april-4-2025-delays-in-starting-workflows/53113"
---
**Symptoms:** Workflow latency spiked, and jobs were dropped after exhausting a 10-minute retry.

**Root cause:** After a blue/green upgrade of the workflows database, the planner's statistics were stale: a second major-version upgrade in the same deployment invalidated an earlier ANALYZE, so every query ran against disk without usable indexes.

**Resolution:** Re-promoted the old (blue) database.

**Source:** CircleCI post-incident report, https://discuss.circleci.com/t/post-incident-report-april-4-2025-delays-in-starting-workflows/53113 (summary via danluu/post-mortems).
