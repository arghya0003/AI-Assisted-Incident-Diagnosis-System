---
incident_id: incident-0101
title: "Cloudflare: WAF rule with a backtracking regular expression exhausts CPU"
fault_type: bad_deploy_latency
source: public_postmortem
source_url: "https://web.archive.org/web/20211006055154/https://blog.cloudflare.com/details-of-the-cloudflare-outage-on-july-2-2019/"
---
**Symptoms:** Cloudflare services went down globally for 27 minutes.

**Root cause:** A single WAF rule, deployed quickly to production, contained a poorly written regular expression that backtracked excessively and exhausted CPU.

**Resolution:** Not described in the summary; see the source.

**Source:** Cloudflare postmortem, https://web.archive.org/web/20211006055154/https://blog.cloudflare.com/details-of-the-cloudflare-outage-on-july-2-2019/ (summary via danluu/post-mortems).
