---
incident_id: incident-0117
title: "Discord: flapping service causes a reconnect storm and memory exhaustion"
fault_type: service_crash
source: public_postmortem
source_url: "https://status.discordapp.com/incidents/dj3l6lw926kl"
---
**Symptoms:** Frontend services ran out of memory.

**Root cause:** A flapping service came back up to a thundering herd of reconnecting clients. Internal queues filled, and the failure cascaded into out-of-memory errors in the frontend services.

**Resolution:** Not described in the summary; see the source.

**Source:** Discord status incident, https://status.discordapp.com/incidents/dj3l6lw926kl (summary via danluu/post-mortems).
