---
incident_id: incident-0033
title: "payment slowed by DNS resolution delays"
services: [payment]
fault_type: dependency_failure
source: synthetic
---
**Symptoms:** payment latency rose intermittently and some orders checkouts timed out. There was no deploy.

**Root cause:** payment's container used a misconfigured DNS resolver that took seconds to resolve some hostnames when opening connections.

**Resolution:** Fixed the resolver configuration and restarted payment.
