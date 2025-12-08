#!/usr/bin/env python3
"""
VPS entrypoint for long-horizon coding agent.

Simplified version of bedrock_entrypoint.py that runs on a VPS without AWS dependencies.
Receives configuration via environment variables and runs the agent directly.

Environment Variables:
    AGENT_PAYLOAD: JSON string with issue details (from GitHub Actions via SSH)
    SESSION_ID: Unique session identifier

    Authentication (choose ONE method):
    - CLAUDE_CODE_OAUTH_TOKEN or CLAUDE_CODE_OAUTH_TOKEN_FILE: OAuth token for Claude subscription (Pro/Max)
    - ANTHROPIC_API_KEY or ANTHROPIC_API_KEY_FILE: API key for Claude (pay-as-you-go)

    GITHUB_TOKEN or GITHUB_TOKEN_FILE: GitHub token for operations
    METRICS_FILE: Path to local metrics JSON file (default: /app/metrics/health.json)
    SESSION_DURATION_HOURS: Maximum session duration (default: 7.0)
"""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, List

# Add src to path for imports
sys.path.insert(0, '/app/src')
sys.path.insert(0, '/app')

# Import GitManager for centralized git operations
try:
    from git_manager import GitManager, GitHubConfig
    GIT_MANAGER_AVAILABLE = True
except ImportError:
    GIT_MANAGER_AVAILABLE = False
    GitManager = None
    GitHubConfig = None
    print("Warning: GitManager not available")

# Import GitHub integration
try:
    from github_integration import GitHubIssueManager
    GITHUB_INTEGRATION_AVAILABLE = True
except ImportError:
    GITHUB_INTEGRATION_AVAILABLE = False
    print("Warning: GitHub integration not available")

# Import local metrics publisher
try:
    from local_metrics import LocalMetricsPublisher
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False
    LocalMetricsPublisher = None
    print("Warning: Local metrics not available")

# Constants
AGENT_BRANCH = "agent-runtime"
EFS_BASE_PATH = Path("/app/workspace")
AGENT_RUNTIME_DIR = EFS_BASE_PATH / "agent-runtime"
BACKLOG_FILE_PATH = AGENT_RUNTIME_DIR / "human_backlog.json"
GITHUB_TOKEN_FILE = "/tmp/github_token.txt"
COMMITS_QUEUE_FILE = "/tmp/commits_queue.txt"

# Global state
agent_process = None
session_start_time = None
uploaded_screenshots: set[str] = set()
announced_commits: set[str] = set()
session_pushed_commits: list[str] = []


def get_secret_from_file_or_env(name: str, env_var: str, file_env_var: str) -> Optional[str]:
    """Get secret from file or environment variable.

    Args:
        name: Name for logging
        env_var: Environment variable name for direct value
        file_env_var: Environment variable name for file path

    Returns:
        Secret value or None
    """
    # Try direct environment variable first
    value = os.environ.get(env_var)
    if value:
        return value

    # Try file-based secret
    file_path = os.environ.get(file_env_var)
    if file_path and Path(file_path).exists():
        try:
            return Path(file_path).read_text().strip()
        except Exception as e:
            print(f"Warning: Failed to read {name} from {file_path}: {e}")

    # Try default paths
    default_paths = [
        f"/app/secrets/{name.lower().replace(' ', '_')}",
        f"/opt/agent/secrets/{name.lower().replace(' ', '_')}",
    ]
    for path in default_paths:
        if Path(path).exists():
            try:
                return Path(path).read_text().strip()
            except Exception:
                pass

    return None


def get_anthropic_api_key() -> Optional[str]:
    """Get Anthropic API key from environment or file."""
    return get_secret_from_file_or_env(
        "anthropic_api_key",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY_FILE"
    )


def get_claude_oauth_token() -> Optional[str]:
    """Get Claude OAuth token from environment or file (for subscription auth)."""
    return get_secret_from_file_or_env(
        "claude_oauth_token",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE"
    )


def get_github_token() -> Optional[str]:
    """Get GitHub token from environment or file."""
    return get_secret_from_file_or_env(
        "github_token",
        "GITHUB_TOKEN",
        "GITHUB_TOKEN_FILE"
    )


def write_github_token_to_file(github_token: str) -> bool:
    """Write GitHub token to file for post-commit hook."""
    try:
        with open(GITHUB_TOKEN_FILE, 'w') as f:
            f.write(github_token)
        os.chmod(GITHUB_TOKEN_FILE, 0o600)
        return True
    except Exception as e:
        print(f"Warning: Failed to write GitHub token file: {e}")
        return False


def setup_post_commit_hook(build_dir: Path, github_repo: str, branch_name: str) -> bool:
    """Set up git post-commit hook for immediate push after commits."""
    hooks_dir = build_dir / ".git" / "hooks"
    hook_path = hooks_dir / "post-commit"

    hooks_dir.mkdir(parents=True, exist_ok=True)

    hook_script = f'''#!/bin/bash
# Git post-commit hook - pushes immediately after each commit
BRANCH_NAME="{branch_name}"
GITHUB_REPO="{github_repo}"
TOKEN_FILE="{GITHUB_TOKEN_FILE}"
COMMITS_QUEUE="{COMMITS_QUEUE_FILE}"

COMMIT_SHA=$(git rev-parse HEAD)
COMMIT_MSG=$(git log -1 --format=%s HEAD)

echo "[post-commit] New commit: ${{COMMIT_SHA:0:12}} - $COMMIT_MSG"

# Read token
if [ ! -f "$TOKEN_FILE" ]; then
    echo "[post-commit] Token file not found, skipping push"
    exit 0
fi
TOKEN=$(cat "$TOKEN_FILE")

# Push to remote
PUSH_URL="https://x-access-token:${{TOKEN}}@github.com/${{GITHUB_REPO}}.git"
if git push "$PUSH_URL" HEAD:${{BRANCH_NAME}} 2>&1; then
    echo "[post-commit] Push successful"
    echo "$COMMIT_SHA" >> "$COMMITS_QUEUE"
else
    echo "[post-commit] Push failed"
fi
'''

    try:
        hook_path.write_text(hook_script)
        os.chmod(hook_path, 0o755)
        print(f"Installed post-commit hook at {hook_path}")
        return True
    except Exception as e:
        print(f"Warning: Failed to set up post-commit hook: {e}")
        return False


def setup_agent_runtime(
    github_repo: str,
    github_token: str,
    issue_number: int
) -> tuple[Path, Optional[Any]]:
    """Set up the agent-runtime workspace by cloning/pulling the repository."""

    EFS_BASE_PATH.mkdir(parents=True, exist_ok=True)

    clone_url = f"https://x-access-token:{github_token}@github.com/{github_repo}.git"

    if AGENT_RUNTIME_DIR.exists() and (AGENT_RUNTIME_DIR / ".git").exists():
        print(f"Updating existing workspace at {AGENT_RUNTIME_DIR}")

        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=AGENT_RUNTIME_DIR,
            capture_output=True
        )

        subprocess.run(
            ["git", "checkout", AGENT_BRANCH],
            cwd=AGENT_RUNTIME_DIR,
            capture_output=True
        )

        subprocess.run(
            ["git", "reset", "--hard", f"origin/{AGENT_BRANCH}"],
            cwd=AGENT_RUNTIME_DIR,
            capture_output=True
        )
    else:
        print(f"Cloning repository to {AGENT_RUNTIME_DIR}")

        if AGENT_RUNTIME_DIR.exists():
            shutil.rmtree(AGENT_RUNTIME_DIR)

        result = subprocess.run(
            ["git", "clone", "-b", AGENT_BRANCH, "--single-branch", clone_url, str(AGENT_RUNTIME_DIR)],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            # Branch might not exist, clone default and create it
            subprocess.run(
                ["git", "clone", clone_url, str(AGENT_RUNTIME_DIR)],
                capture_output=True
            )
            subprocess.run(
                ["git", "checkout", "-b", AGENT_BRANCH],
                cwd=AGENT_RUNTIME_DIR,
                capture_output=True
            )

    # Configure git
    subprocess.run(
        ["git", "config", "user.email", "agent@claude-code.local"],
        cwd=AGENT_RUNTIME_DIR
    )
    subprocess.run(
        ["git", "config", "user.name", "Claude Code Agent"],
        cwd=AGENT_RUNTIME_DIR
    )

    # Set up post-commit hook
    write_github_token_to_file(github_token)
    setup_post_commit_hook(AGENT_RUNTIME_DIR, github_repo, AGENT_BRANCH)

    # Initialize GitManager if available
    git_manager = None
    if GIT_MANAGER_AVAILABLE and GitManager:
        config = GitHubConfig(
            repo=github_repo,
            token=github_token,
            branch=AGENT_BRANCH
        )
        git_manager = GitManager(AGENT_RUNTIME_DIR, config)
        print("GitManager initialized")

    return AGENT_RUNTIME_DIR, git_manager


def write_session_state(
    workspace_dir: Path,
    session_id: str,
    current_issue: int,
    status: str = "running"
):
    """Write session state to local file (replaces SSM Parameter Store)."""
    state = {
        "session_id": session_id,
        "current_issue": current_issue,
        "status": status,
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "working_directory": str(workspace_dir)
    }

    state_path = workspace_dir / "session_state.json"
    state_path.write_text(json.dumps(state, indent=2))


def read_session_state(workspace_dir: Path) -> Optional[Dict]:
    """Read session state from local file."""
    state_path = workspace_dir / "session_state.json"

    if not state_path.exists():
        return None

    try:
        return json.loads(state_path.read_text())
    except Exception:
        return None


def run_agent(
    build_dir: Path,
    auth_token: str,
    auth_type: str = "api_key",
    is_enhancement: bool = False,
    feature_request_path: Optional[Path] = None
):
    """Run the Claude Code agent.

    Args:
        build_dir: Working directory for the agent
        auth_token: Authentication token (API key or OAuth token)
        auth_type: "api_key" for ANTHROPIC_API_KEY, "oauth" for CLAUDE_CODE_OAUTH_TOKEN
        is_enhancement: Whether this is an enhancement session
        feature_request_path: Path to feature request file for enhancement mode
    """
    global agent_process

    model = os.environ.get("DEFAULT_MODEL", "claude-opus-4-5-20251101")
    project_name = os.environ.get("PROJECT_NAME", "canopy")

    if is_enhancement and feature_request_path:
        cmd = [
            "python", "/app/claude_code.py",
            "--enhance-feature", str(feature_request_path),
            "--existing-codebase", str(build_dir / "generated-app"),
            "--model", model,
            "--skip-git-init"
        ]
    else:
        cmd = [
            "python", "/app/claude_code.py",
            "--project", project_name,
            "--model", model,
            "--output-dir", str(build_dir / "generated-app"),
            "--skip-git-init"
        ]

    print(f"Running agent: {' '.join(cmd)}")
    print(f"Working directory: {build_dir}")
    print(f"Authentication: {auth_type}")

    env = os.environ.copy()

    # Set authentication based on type
    if auth_type == "oauth":
        # Use OAuth token for subscription-based authentication
        env['CLAUDE_CODE_OAUTH_TOKEN'] = auth_token
        # Ensure API key is NOT set (it would override OAuth)
        env.pop('ANTHROPIC_API_KEY', None)
        print("Using Claude subscription (OAuth token)")
    else:
        # Use API key for pay-as-you-go authentication
        env['ANTHROPIC_API_KEY'] = auth_token
        # Ensure OAuth token is NOT set
        env.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
        print("Using Anthropic API key")

    agent_process = subprocess.Popen(
        cmd,
        cwd=str(build_dir),
        env=env
    )

    print(f"Agent started (PID: {agent_process.pid})")

    # Wait for completion
    agent_process.wait()

    print(f"Agent completed with exit code: {agent_process.returncode}")
    return agent_process.returncode


def post_commits_to_issue(
    github_repo: str,
    github_token: str,
    issue_number: int,
    build_dir: Path,
    branch_name: str
) -> bool:
    """Post commit summary to GitHub issue."""
    try:
        from github import Github

        gh = Github(github_token)
        repo = gh.get_repo(github_repo)
        issue = repo.get_issue(issue_number)

        # Get commits not yet announced
        result = subprocess.run(
            ["git", "log", "--oneline", "-20"],
            cwd=build_dir,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            return False

        commits = result.stdout.strip().split('\n')
        if not commits or commits == ['']:
            return True  # No commits to announce

        commit_lines = []
        for commit in commits[:10]:
            parts = commit.split(' ', 1)
            sha = parts[0]
            msg = parts[1] if len(parts) > 1 else ""
            commit_url = f"https://github.com/{github_repo}/commit/{sha}"
            commit_lines.append(f"- [`{sha}`]({commit_url}) {msg}")

        branch_url = f"https://github.com/{github_repo}/tree/{branch_name}"
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        comment = f"""**Commits Pushed** ({timestamp})

Branch: [`{branch_name}`]({branch_url})

{chr(10).join(commit_lines)}
"""
        issue.create_comment(comment)
        print(f"Posted commit summary to issue #{issue_number}")
        return True

    except Exception as e:
        print(f"Warning: Failed to post commits to issue: {e}")
        return False


def main():
    """Main entry point for VPS-based agent."""
    global session_start_time

    print("=" * 80)
    print("VPS Agent Entrypoint")
    print("=" * 80)

    # Parse payload from environment
    payload_str = os.environ.get("AGENT_PAYLOAD", "{}")
    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid AGENT_PAYLOAD JSON: {e}")
        return 1

    session_id = os.environ.get("SESSION_ID", f"vps-{int(time.time())}")

    print(f"Session ID: {session_id}")
    print(f"Payload: {json.dumps(payload, indent=2)}")

    # Get credentials - prefer OAuth token (subscription) over API key
    oauth_token = get_claude_oauth_token()
    api_key = get_anthropic_api_key()

    if oauth_token:
        auth_token = oauth_token
        auth_type = "oauth"
        print("Found Claude OAuth token (subscription authentication)")
    elif api_key:
        auth_token = api_key
        auth_type = "api_key"
        print("Found Anthropic API key (pay-as-you-go authentication)")
    else:
        print("Error: No authentication found")
        print("  Set CLAUDE_CODE_OAUTH_TOKEN for subscription auth")
        print("  Or ANTHROPIC_API_KEY for API key auth")
        return 1

    github_token = get_github_token()
    if not github_token:
        print("Error: GitHub token not found")
        return 1

    # Initialize metrics publisher
    metrics_publisher = None
    if METRICS_AVAILABLE and LocalMetricsPublisher:
        metrics_file = os.environ.get("METRICS_FILE", "/app/metrics/health.json")
        metrics_publisher = LocalMetricsPublisher(
            issue_number=payload.get('issue_number'),
            session_id=session_id,
            metrics_file=metrics_file
        )
        print(f"Metrics enabled: {metrics_file}")

    # Extract payload details
    mode = payload.get('mode', 'build-from-issue')
    issue_number = payload.get('issue_number')
    github_repo = payload.get('github_repo')
    issue_title = payload.get('issue_title', f'Issue #{issue_number}')
    issue_body = payload.get('issue_body', '')
    resume_session = payload.get('resume_session', False)

    if not issue_number or not github_repo:
        print("Error: Missing issue_number or github_repo in payload")
        return 1

    print(f"Building issue #{issue_number} from {github_repo}")

    # Initialize session
    session_start_time = time.time()
    session_duration = float(os.environ.get("SESSION_DURATION_HOURS", "7.0")) * 3600

    # Publish session start
    if metrics_publisher:
        metrics_publisher.publish_session_started(mode="full_build")

    try:
        # Set up workspace
        build_dir, git_manager = setup_agent_runtime(
            github_repo=github_repo,
            github_token=github_token,
            issue_number=issue_number
        )

        # Write session state
        write_session_state(build_dir, session_id, issue_number, "running")

        # Check if this is an enhancement
        is_enhancement = (build_dir / "generated-app" / "package.json").exists()

        # Write feature request
        feature_request = f"""# Feature Request: Issue #{issue_number}

## Title
{issue_title}

## Description
{issue_body}

## Branch
All work should be committed to the `{AGENT_BRANCH}` branch.
Commits should reference this issue: `Ref: #{issue_number}`

## Mode
{"Enhancement" if is_enhancement else "Full Build"}
"""
        feature_request_path = build_dir / "FEATURE_REQUEST.md"
        feature_request_path.write_text(feature_request)

        print(f"Setup complete. Mode: {'enhancement' if is_enhancement else 'full build'}")

        # Start heartbeat thread
        stop_heartbeat = threading.Event()

        def heartbeat_loop():
            while not stop_heartbeat.is_set():
                if metrics_publisher:
                    elapsed = time.time() - session_start_time
                    remaining = max(0, session_duration - elapsed)
                    metrics_publisher.publish_progress(
                        elapsed_hours=elapsed / 3600,
                        remaining_hours=remaining / 3600
                    )
                    metrics_publisher.publish_session_heartbeat()
                stop_heartbeat.wait(60)  # Every 60 seconds

        heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        heartbeat_thread.start()

        # Run the agent
        exit_code = run_agent(
            build_dir=build_dir,
            auth_token=auth_token,
            auth_type=auth_type,
            is_enhancement=is_enhancement,
            feature_request_path=feature_request_path if is_enhancement else None
        )

        # Stop heartbeat
        stop_heartbeat.set()

        # Post commits to issue
        post_commits_to_issue(
            github_repo=github_repo,
            github_token=github_token,
            issue_number=issue_number,
            build_dir=build_dir,
            branch_name=AGENT_BRANCH
        )

        # Update session state
        write_session_state(build_dir, session_id, issue_number, "completed")

        # Publish completion
        if metrics_publisher:
            duration = time.time() - session_start_time
            metrics_publisher.publish_session_completed(exit_code, duration)

        return exit_code

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

        if metrics_publisher:
            metrics_publisher.publish_error(str(type(e).__name__))

        return 1


if __name__ == "__main__":
    sys.exit(main())
