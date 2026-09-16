---
incident_id: incident-0125
title: "crates.io: deployment bug breaks download URL generation"
fault_type: bad_deploy_errors
source: public_postmortem
source_url: "https://blog.rust-lang.org/inside-rust/2023/07/21/crates-io-postmortem.html"
---
**Symptoms:** All crate downloads failed for about 13 minutes, roughly 3.7 million failed requests including client retries.

**Root cause:** A deployment contained a bug in download URL generation.

**Resolution:** Not described in the summary; see the source.

**Source:** Rust blog, https://blog.rust-lang.org/inside-rust/2023/07/21/crates-io-postmortem.html (summary via danluu/post-mortems).
