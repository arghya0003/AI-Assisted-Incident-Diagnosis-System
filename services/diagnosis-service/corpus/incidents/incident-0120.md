---
incident_id: incident-0120
title: "Spotify: missing exponential backoff causes a cascading failure"
fault_type: dependency_failure
source: public_postmortem
source_url: "https://labs.spotify.com/2013/06/04/incident-management-at-spotify/"
---
**Symptoms:** Notable service degradation.

**Root cause:** A microservice lacked exponential backoff on retries, which caused a cascading failure.

**Resolution:** Not described in the summary; see the source.

**Source:** Spotify Labs, https://labs.spotify.com/2013/06/04/incident-management-at-spotify/ (summary via danluu/post-mortems).
