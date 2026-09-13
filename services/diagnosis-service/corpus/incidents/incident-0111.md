---
incident_id: incident-0111
title: "GitHub: shorter cache TTL overwhelms the auth database and cascades"
fault_type: db_contention
source: public_postmortem
source_url: "https://github.blog/news-insights/company-news/addressing-githubs-recent-availability-issues-2/"
---
**Symptoms:** At Monday peak, failures spread across github.com, the API, Actions, Git over HTTPS, Copilot and other dependent services.

**Root cause:** Two client apps had quietly grown read traffic tenfold, then a change shortened a user-settings cache TTL from 12 hours to 2. Cache rewrites plus read load overwhelmed the core auth and user-management database cluster, and the failure cascaded to every service depending on it.

**Resolution:** Not described in the summary; see the source.

**Source:** GitHub blog, https://github.blog/news-insights/company-news/addressing-githubs-recent-availability-issues-2/ (summary via danluu/post-mortems).
