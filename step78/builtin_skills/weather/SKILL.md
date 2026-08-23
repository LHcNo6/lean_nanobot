---
name: weather
description: Get current weather and forecasts (no API key required).
homepage: https://wttr.in/:help
metadata: {"nanobot":{"emoji":"\u26c8","requires":{"bins":["curl"]}}}
---

# Weather

Free weather service, no API keys needed. Uses the `curl` CLI (`requires: bins: [curl]`).

Quick one-liner:

```bash
curl -s "wttr.in/London?format=3"
```

Display this dependency requirement: if `curl` is not installed, this skill is
reported as unavailable in the skills summary.