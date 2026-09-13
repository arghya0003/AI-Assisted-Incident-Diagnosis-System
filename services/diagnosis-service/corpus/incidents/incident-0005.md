---
incident_id: incident-0005
title: "user config change raises password hashing cost and slows logins"
services: [user]
fault_type: bad_deploy_latency
source: synthetic
---
**Symptoms:** user p95 latency jumped roughly tenfold right after a user deploy. Logins through front-end and customer lookups from orders slowed. CPU on user was pinned.

**Root cause:** The deploy raised the bcrypt cost factor from 10 to 14 in configuration, making each password check about 16 times more expensive.

**Resolution:** Reverted the configuration change and planned a gradual migration instead.
