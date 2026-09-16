---
incident_id: incident-0114
title: "incident.io: audit extension hangs while holding database locks after a migration"
fault_type: db_contention
source: public_postmortem
source_url: "https://status.incident.io/incidents/01JRDFKAGE07YYDY0KZR137BX3/write-up"
---
**Symptoms:** Database operations were blocked across the dashboard, mobile app, Slack app and API.

**Root cause:** After a Postgres 17 upgrade, PGAudit was re-enabled. A routine migration interacted badly with it, and the extension hung while holding critical locks and ignored timeout signals.

**Resolution:** Restarted the primary to break the deadlock (about two minutes of hard outage) and removed PGAudit.

**Source:** incident.io write-up, https://status.incident.io/incidents/01JRDFKAGE07YYDY0KZR137BX3/write-up (summary via danluu/post-mortems).
