---
incident_id: incident-0126
title: "Heroku: deployment process ignores newly added config variables"
fault_type: bad_deploy_errors
source: public_postmortem
source_url: "https://blog.heroku.com/how-i-broke-git-push-heroku-main"
---
**Symptoms:** Code that required new config variables ran without them.

**Root cause:** An incorrect deployment process meant new config variables were not used when the code needed them.

**Resolution:** Not described in the summary; see the source.

**Source:** Heroku blog, https://blog.heroku.com/how-i-broke-git-push-heroku-main (summary via danluu/post-mortems).
