---
incident_id: incident-0103
title: "Cloudflare: deployment tool points production at a staging build"
fault_type: bad_deploy_errors
source: public_postmortem
source_url: "https://blog.cloudflare.com/cloudflare-incident-on-october-30-2023/"
---
**Symptoms:** Requests failed with HTTP 401 errors, cascading into failures across Workers KV, Pages, Access and Turnstile.

**Root cause:** A bug in the deployment tool made the production environment reference a staging build version, which routed traffic to an endpoint that rejected it.

**Resolution:** Not described in the summary; see the source.

**Source:** Cloudflare postmortem, https://blog.cloudflare.com/cloudflare-incident-on-october-30-2023/ (summary via danluu/post-mortems).
