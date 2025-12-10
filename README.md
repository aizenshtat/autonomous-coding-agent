# Long-Horizon Coding Agent Demo

An autonomous coding agent system that builds complete applications from specifications. The agent takes a project spec, creates a repository, and autonomously implements the entire application over multiple sessions.

## What This Does

```
Specification (BUILD_PLAN.md) → Agent → Complete Application
```

1. **You provide**: A detailed project specification
2. **Agent creates**: Repository from template, 200+ test cases, full implementation
3. **Agent iterates**: Multiple sessions until all tests pass
4. **Result**: Production-ready application with CI/CD integration

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 20+ and pnpm 9+
- Docker (for database)
- Anthropic API key

### Run Locally

```bash
# 1. Set your API key
export ANTHROPIC_API_KEY="sk-ant-..."

# 2. Run the agent (uses default 'canopy' project)
python agent/claude_code.py

# Or specify a custom project
python agent/claude_code.py --project myproject
```

The agent will:
1. Create `generated-app/` directory
2. Clone the template from `templates/product-repo/`
3. Copy the spec from `specs/<project>/BUILD_PLAN.md`
4. Start Claude and build the entire app autonomously
5. Create `tests.json` with 200+ test cases
6. Loop sessions until all tests pass

### Resume After Interruption

```bash
# Just run the same command - agent auto-resumes
python agent/claude_code.py
```

## Project Structure

```
.
├── agent/                      # Autonomous coding agent
│   ├── claude_code.py          # Main orchestrator (1,816 lines)
│   ├── vps_entrypoint.py       # VPS deployment wrapper
│   ├── src/                    # Agent modules
│   │   ├── config.py           # Configuration constants
│   │   ├── git_manager.py      # Git operations
│   │   ├── github_integration.py # GitHub issue lifecycle
│   │   ├── session_manager.py  # Session setup
│   │   └── token_tracker.py    # Cost tracking
│   ├── prompts/                # Agent instructions
│   │   └── system_prompt.txt   # Core system prompt
│   └── deployment/             # Docker configs
│       └── Dockerfile
│
├── specs/                      # Project specifications
│   └── canopy/                 # Example: JIRA-like app
│       └── BUILD_PLAN.md       # 1,686-line specification
│
├── templates/                  # Application templates
│   └── product-repo/           # Full-stack monorepo
│       ├── apps/web/           # React + Vite + Tailwind
│       ├── apps/api/           # Express + Prisma + PostgreSQL
│       └── packages/shared/    # Shared TypeScript types
│
└── generated-app/              # Output (created by agent)
    ├── agent_state.json        # State machine control
    ├── claude-progress.txt     # Session notes
    ├── tests.json              # 200+ test definitions
    └── src/                    # Generated application code
```

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes* | Anthropic API key |
| `CLAUDE_CODE_OAUTH_TOKEN` | Yes* | OAuth token (alternative) |
| `GITHUB_TOKEN` | VPS only | GitHub API token |
| `GITHUB_REPO` | VPS only | Repository (owner/repo) |
| `DEFAULT_MODEL` | No | Model (default: claude-opus-4-5-20251101) |
| `DEFAULT_FRONTEND_PORT` | No | Frontend port (default: 6174) |
| `DEFAULT_BACKEND_PORT` | No | Backend port (default: 4001) |

*One of `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` is required

### Command Line Options

```bash
python agent/claude_code.py [OPTIONS]

Options:
  --project NAME       Project spec to use (default: canopy)
  --output-dir PATH    Output directory (default: ./generated-app)
  --model MODEL_ID     Claude model to use
  --start-paused       Start in paused state
  --print-prompts      Print prompts without running
```

## Agent State Management

Control the agent via `generated-app/agent_state.json`:

```json
{
  "desired_state": "continuous",
  "current_state": "running",
  "timestamp": "2025-12-10T12:00:00Z"
}
```

| State | Behavior |
|-------|----------|
| `continuous` | Runs sessions until completion detected |
| `run_once` | Runs 1 session, then pauses |
| `run_cleanup` | Runs cleanup (removes tech debt), then pauses |
| `pause` | Waits for state change |

## Creating Custom Projects

1. Create a new spec directory:
   ```bash
   mkdir -p specs/my-project
   ```

2. Create `BUILD_PLAN.md` with your specification:
   ```markdown
   # My Project

   ## Overview
   Description of what to build...

   ## Features
   - Feature 1: ...
   - Feature 2: ...

   ## Technical Requirements
   - Stack: React, Express, PostgreSQL
   - ...
   ```

3. Run the agent:
   ```bash
   python agent/claude_code.py --project my-project
   ```

See `specs/canopy/BUILD_PLAN.md` for a comprehensive example (1,686 lines).

## VPS Deployment (Zero-Setup)

Deploy the agent to any VPS (Hetzner, DigitalOcean, Linode, etc.) with a single command.

### One-Click Bootstrap

Run this on your VPS as root:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/agent/deployment/bootstrap.sh | \
  bash -s -- --domain agent.example.com --email admin@example.com
```

This automatically:
- Installs Docker, Nginx, Certbot (only if missing)
- Creates deploy user with SSH key
- Obtains SSL certificate
- Configures Nginx for previews
- Sets up systemd service

### Configure GitHub Secrets

After bootstrap completes, add these secrets to your GitHub repository:

| Secret | Value |
|--------|-------|
| `VPS_HOST` | Your domain (e.g., `agent.example.com`) |
| `VPS_USER` | `deploy` |
| `VPS_SSH_KEY` | Private key (output by bootstrap) |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `AGENT_GITHUB_TOKEN` | GitHub PAT for agent operations |

### Trigger a Build

1. Push to `main` branch (deploys infrastructure)
2. Create a GitHub issue with your feature request
3. Add a rocket emoji reaction to the issue
4. Agent starts within 5 minutes
5. Preview available at `https://agent.example.com/previews/issue-{N}/`

### Endpoints

After deployment:

| Endpoint | Purpose |
|----------|---------|
| `https://domain/health` | Health check |
| `https://domain/metrics/health.json` | Agent metrics |
| `https://domain/previews/` | Preview deployments |

### Manual Docker Run (Alternative)

If you prefer manual control:

```bash
docker build -f agent/deployment/Dockerfile -t claude-code-agent:latest .

docker run \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  -e GITHUB_TOKEN="ghp_..." \
  -e AGENT_PAYLOAD='{"mode":"build-from-issue","issue_number":42,"github_repo":"owner/repo"}' \
  -v $(pwd)/workspace:/app/workspace \
  claude-code-agent:latest
```

## Monitoring & Debugging

### View Progress

```bash
# Session notes
cat generated-app/claude-progress.txt

# Failing tests
cat generated-app/tests.json | jq '.[] | select(.passes==false)'

# Session logs
ls generated-app/logs/
```

### Token Usage

The agent tracks token usage and costs. After each session:
```
Input Tokens: 125,000 (including 45,000 from cache)
Output Tokens: 85,000
Total Cost: $3.45 / $5,000 limit
```

## Running the Generated App

After the agent completes:

```bash
cd generated-app

# Install dependencies
pnpm install

# Start database
docker-compose up -d

# Initialize database
pnpm db:push

# Run development server
pnpm dev
```

- Frontend: http://localhost:6174
- Backend API: http://localhost:4001

## Example: Canopy Project

The included `canopy` spec builds a JIRA-like project management app:

- Project management with archiving
- Issues: Epic, Story, Bug, Task, Sub-task
- Sprint planning with velocity tracking
- Kanban boards with drag-and-drop
- Backlog prioritization
- Roadmap timeline view
- Burndown and velocity charts
- Full-text search
- Dark mode support

## Tech Stack

**Agent (Python)**
- claude-agent-sdk
- PyGithub
- aiofiles

**Generated Apps (Node.js)**
- React 19 + Vite + TypeScript
- Tailwind CSS v4
- Express + Prisma + PostgreSQL
- Playwright for E2E tests

## License

MIT
