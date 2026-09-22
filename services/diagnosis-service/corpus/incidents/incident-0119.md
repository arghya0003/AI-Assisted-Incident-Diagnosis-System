---
incident_id: incident-0119
title: "Allegro: cluster config prevents scaling during a traffic spike"
fault_type: capacity
source: public_postmortem
source_url: "https://allegro.tech/2018/08/postmortem-why-allegro-went-down.html"
---
**Symptoms:** The e-commerce site went down during a sudden traffic spike caused by a marketing campaign.

**Root cause:** A configuration error in cluster resource management prevented more service instances from starting, even though hardware resources were available.

**Resolution:** Not described in the summary; see the source.

**Source:** Allegro postmortem, https://allegro.tech/2018/08/postmortem-why-allegro-went-down.html (summary via danluu/post-mortems).
