# Javis OS

**A self-hosted agentic AI system with switchable models, a Second Brain, and automation.**

[Vietnamese documentation](README.md) · [English documentation](docs/en/README.md)

## What is Javis?

Javis provides chat, file operations, MCP connections, skills, background work, and durable knowledge. The model can be changed without replacing the surrounding tools, workflows, or user data.

Javis supports **10 providers** and groups models into **7 groups** so users can choose a suitable route without losing the surrounding capabilities.

## Install and deploy

- Docker Compose files are in [`deploy/docker/`](deploy/docker/).
- Linux and macOS installer: [`deploy/linux/install.sh`](deploy/linux/install.sh).
- Windows setup: [`deploy/windows/setup.bat`](deploy/windows/setup.bat). After setup, open [`JAVIS OS.bat`](deploy/windows/JAVIS OS.bat) and enable auto-start with `javis-autostart.bat install` when needed.

See [DEPLOY.md](DEPLOY.md) for deployment details. Do not remove Docker volumes `/data` or `/brains` during an update; they hold runtime state and Second Brain data.
