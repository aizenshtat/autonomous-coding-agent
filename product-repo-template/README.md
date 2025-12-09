# Product Repository Template

This directory contains GitHub workflows and configuration that should be copied to each product repository created by the autonomous agent.

## Contents

### `.github/workflows/`

- **`deploy-preview.yml`** - Deploys the generated app to a preview URL when commits are pushed to `agent-runtime` branch
- **`spec-bot.yml`** - Handles user feature requests via GitHub Issues (spec refinement, test generation)

## Setup

When the agent creates a new product repo, these workflows are automatically copied. They require the following secrets to be configured in the product repo:

### Required Secrets

| Secret | Description |
|--------|-------------|
| `VPS_HOST` | VPS hostname or IP address |
| `VPS_SSH_KEY` | SSH private key for deployment |
| `VPS_SSH_USER` | SSH username (usually `deploy`) |
| `ANTHROPIC_API_KEY` | API key for spec-bot Claude calls |

### Required Variables

| Variable | Description |
|----------|-------------|
| `PREVIEWS_DOMAIN` | Domain for preview deployments (e.g., `previews.example.com`) |
| `AUTHORIZED_APPROVERS` | Comma-separated GitHub usernames who can approve features |

## How It Works

1. **Agent creates product repo** with these workflows
2. **Agent builds app** → pushes to `agent-runtime` branch
3. **deploy-preview.yml** triggers → deploys to preview URL
4. **User creates issue** with feature request
5. **spec-bot.yml** triggers → refines spec, generates tests
6. **Agent picks up new tests** → implements feature → cycle continues
