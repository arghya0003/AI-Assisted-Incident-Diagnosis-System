---
incident_id: incident-0105
title: "CircleCI: deploy changes a database field type and job distribution stops"
fault_type: bad_deploy_errors
source: public_postmortem
source_url: "https://discuss.circleci.com/t/incident-report-november-8-2021-jobs-stuck-in-a-not-running-state/41890"
---
**Symptoms:** Jobs were stuck in a not-running state because the job-distribution service stopped distributing work.

**Root cause:** A deploy changed the type of a field in the PostgreSQL database the distributor uses. Old and new rows then had different types, and the distributor's strict schema validation failed on every scan.

**Resolution:** Rolling back did not help, because rows written between the two deploys became unreadable. Recovery needed a hand-deployed build that ignored the field, plus manual scaling. A rollback is not always safe once data has changed.

**Source:** CircleCI incident report, https://discuss.circleci.com/t/incident-report-november-8-2021-jobs-stuck-in-a-not-running-state/41890 (summary via danluu/post-mortems).
