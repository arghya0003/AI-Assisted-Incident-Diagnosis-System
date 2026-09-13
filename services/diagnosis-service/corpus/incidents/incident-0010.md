---
incident_id: incident-0010
title: "catalogue release ships a query incompatible with the database schema"
services: [catalogue]
fault_type: bad_deploy_errors
source: synthetic
---
**Symptoms:** catalogue returned HTTP 500 for tag searches right after a catalogue deploy, and front-end search pages errored. catalogue-db logged unknown-column errors.

**Root cause:** The release referenced a column added by a migration that had not yet run in production.

**Resolution:** Rolled back catalogue; the migration and release order were fixed.
