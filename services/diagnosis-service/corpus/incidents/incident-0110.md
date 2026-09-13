---
incident_id: incident-0110
title: "GitHub: connection-saturation config rollout triggers a database failover"
fault_type: config_error
source: public_postmortem
source_url: "https://github.blog/news-insights/company-news/addressing-githubs-recent-availability-issues/"
---
**Symptoms:** Pull-request and push consistency was degraded for more than 10 hours.

**Root cause:** A connection-saturation configuration rolled out to the Git database triggered a failover, and the rollback then failed because of an internal infrastructure error.

**Resolution:** Not described in the summary; see the source.

**Source:** GitHub blog, https://github.blog/news-insights/company-news/addressing-githubs-recent-availability-issues/ (summary via danluu/post-mortems).
