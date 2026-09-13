---
incident_id: incident-0109
title: "GitHub: ProxySQL meltdowns from a silently capped file-descriptor limit"
fault_type: db_pool_saturation
source: public_postmortem
source_url: "https://github.blog/news-insights/company-news/february-service-disruptions-post-incident-analysis/"
---
**Symptoms:** The mysql1 cluster suffered four ProxySQL meltdowns over nine days, 8h14m of impact in total.

**Root cause:** First an analytics query hit the primary instead of replicas, and a planned promotion recreated that failure. Two later load-driven incidents revealed that systemd had silently capped LimitNOFILE to 65,536 because of a kernel-level limit.

**Resolution:** Not described in the summary; see the source.

**Source:** GitHub post-incident analysis, https://github.blog/news-insights/company-news/february-service-disruptions-post-incident-analysis/ (summary via danluu/post-mortems).
