---
incident_id: incident-0014
title: "payment process killed; checkouts fail"
services: [payment]
fault_type: service_crash
source: synthetic
---
**Symptoms:** orders error rate rose on checkout requests and front-end showed failed orders. Browsing, baskets and login were unaffected. payment metrics were absent.

**Root cause:** The payment process died after a fatal native library error and had no automatic restart policy.

**Resolution:** Restarted payment and enabled an automatic restart policy.
