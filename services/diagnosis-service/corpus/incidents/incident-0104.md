---
incident_id: incident-0104
title: "Cloudflare: global config change triggers a proxy exception and HTTP 500s"
fault_type: config_error
source: public_postmortem
source_url: "https://blog.cloudflare.com/5-december-2025-outage/"
---
**Symptoms:** Customers using the Cloudflare Managed Ruleset received HTTP 500 errors.

**Root cause:** A configuration change to disable an internal WAF testing tool was propagated globally without a gradual rollout and triggered a Lua exception in the proxy's rulesets module.

**Resolution:** Not described in the summary; see the source.

**Source:** Cloudflare postmortem, https://blog.cloudflare.com/5-december-2025-outage/ (summary via danluu/post-mortems).
