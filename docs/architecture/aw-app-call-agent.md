---
repo: architecture
path: docs/architecture/aw-app-call-agent.md
source: generated
edited: false
checksum: sha256:c7ee32639e2c1ff275abf14e500f0b7b5b3410f1c1e97a3aa116f44b67ea2bcb
---
# Call Agent

- **repo**: aw-app-call-agent
- **layer**: app
- **technologies**: python, react
- **health** (derived): planned

Talk to your workspace agent out loud. Open the Call window, hit call, and speak — your voice is transcribed, sent to the agent you picked, and the reply is streamed back and spoken to you in your own language. Keeps the conversation going across calls instead of starting from scratch every time.

## Connections
- `http` → **aw-workspace** — routes mounted at /api/apps/call-agent
- `other` → **aw-app-agents-platform-runners** — Holds the agents-platform base URL and identity token this app falls back to when its own are blank, and runs the agent CLI a call is dispatched to

## MCP tools
_none exposed_

## Requirements
_none documented_
