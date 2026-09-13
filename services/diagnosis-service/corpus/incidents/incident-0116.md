---
incident_id: incident-0116
title: "AppNexus: double free triggered by a data update crashes every server at once"
fault_type: service_crash
source: public_postmortem
source_url: "https://web.archive.org/web/20250505112812/https://medium.com/xandr-tech/2013-09-17-outage-postmortem-586b19ae4307"
---
**Symptoms:** All impression bus servers crashed simultaneously.

**Root cause:** A database update exposed a double-free bug. It was not caught in staging because triggering it needed a time delay that the staging period did not include.

**Resolution:** Not described in the summary; see the source.

**Source:** AppNexus postmortem, https://web.archive.org/web/20250505112812/https://medium.com/xandr-tech/2013-09-17-outage-postmortem-586b19ae4307 (summary via danluu/post-mortems).
