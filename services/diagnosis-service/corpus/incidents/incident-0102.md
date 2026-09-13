---
incident_id: incident-0102
title: "Cloudflare: new DDoS rule sends request handlers into 100% CPU loops"
fault_type: bad_deploy_latency
source: public_postmortem
source_url: "https://blog.cloudflare.com/cloudflare-incident-on-june-20-2024/"
---
**Symptoms:** HTTP request handler processes consumed 100% CPU, and the problem spread across data centers.

**Root cause:** A newly deployed DDoS mitigation rule exposed a latent bug in the rate-limiting system's cookie validation logic, which put handler processes into infinite loops.

**Resolution:** Not described in the summary; see the source.

**Source:** Cloudflare postmortem, https://blog.cloudflare.com/cloudflare-incident-on-june-20-2024/ (summary via danluu/post-mortems).
