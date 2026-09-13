---
incident_id: incident-0112
title: "GitHub: database config change breaks health checks and takes reads offline"
fault_type: config_error
source: public_postmortem
source_url: "https://github.blog/news-insights/company-news/github-availability-report-august-2024/"
---
**Symptoms:** The whole site was down for read operations for 36 minutes.

**Root cause:** A configuration change rolled out to the databases broke how hosts answered routing-service health-check pings, so the production read-only endpoint was marked unhealthy and became inaccessible.

**Resolution:** Reverted the change.

**Source:** GitHub availability report, https://github.blog/news-insights/company-news/github-availability-report-august-2024/ (summary via danluu/post-mortems).
