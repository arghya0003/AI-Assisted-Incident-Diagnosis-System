# Past-incident corpus

Retrieval (`app/retrieval.py`) searches these write-ups for incidents similar to a new anomaly.
`corpus/ingest.py` embeds them with `nomic-embed-text` and loads them into the `incidents` table.

## Format

One file per incident, `incidents/incident-NNNN.md`, with YAML front matter and three body sections:

```markdown
---
incident_id: incident-0001
title: "short description"
services: [catalogue]          # where the root cause was; not every service with symptoms
fault_type: bad_deploy_latency # see CorpusFaultType in app/corpus.py
source: synthetic              # or public_postmortem, which also needs source_url
---
**Symptoms:** ...

**Root cause:** ...

**Resolution:** ...
```

`services` names the root cause only, because candidate scoring boosts the services an incident
names. Listing a symptom there would boost a symptom.

## Sources

| Range | Source | Count |
| --- | --- | --- |
| `incident-0001`–`0035` | **Synthetic**, written for this project against Sock Shop service names. They cover the fault injector's three fault types plus deploy errors, capacity, config errors, dependency failures and harmless alerts. | 35 |
| `incident-0101`–`0126` | **Public postmortems**, chosen from [danluu/post-mortems](https://github.com/danluu/post-mortems) for relevance to deploys, crashes and database connection problems. | 26 |

The public entries are short paraphrases of the one-paragraph summaries in that list, each linking
the original postmortem. The list has no licence file, so nothing is copied verbatim, and nothing
is added beyond what the summary states. Where the summary doesn't describe a resolution, the
entry says so. Public entries name no Sock Shop services, so they never boost a candidate's
score; they are retrieved as context for the LLM prompt.

## Caveat for evaluation

The synthetic incidents were written by the same person, from the same fault-injector fault types,
as the test fixtures. Retrieval quality measured on those fixtures is therefore optimistic. The
Week 8 evaluation should report retrieval on real injected faults, or hold out part of this
corpus.
