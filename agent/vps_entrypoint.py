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
from typing import Any, Optional

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
    from github_integration import GitHubIssueManager  # noqa: F401
    GITHUB_INTEGRATION_AVAILABLE = True
except ImportError:
    GITHUB_INTEGRATION_AVAILABLE = False
    GitHubIssueManager = None  # noqa: F811
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


def read_session_state(workspace_dir: Path) -> Optional[dict]:
    """Read session state from local file."""
    state_path = workspace_dir / "session_state.json"

    if not state_path.exists():
        return None

    try:
        return json.loads(state_path.read_text())
    except Exception:
        return None


def run_agent_with_monitoring(
    build_dir: Path,
    auth_token: str,
    auth_type: str,
    github_repo: str,
    github_token: str,
    is_enhancement: bool = False,
    feature_request_path: Optional[Path] = None,
    progress_interval: int = 120
) -> int:
    """Run the Claude Code agent with progress monitoring.

    This function runs the agent in continuous mode and monitors tests.json
    for progress, posting updates to feature issues as tests pass.

    Args:
        build_dir: Working directory for the agent
        auth_token: Authentication token (API key or OAuth token)
        auth_type: "api_key" for ANTHROPIC_API_KEY, "oauth" for CLAUDE_CODE_OAUTH_TOKEN
        github_repo: Repository in format "owner/repo"
        github_token: GitHub token for posting progress
        is_enhancement: Whether this is an enhancement session
        feature_request_path: Path to feature request file for enhancement mode
        progress_interval: Seconds between progress checks (default: 120)

    Returns:
        Agent exit code
    """
    global agent_process

    model = os.environ.get("DEFAULT_MODEL", "claude-opus-4-5-20251101")
    project_name = os.environ.get("PROJECT_NAME", "canopy")

    tests_json_path = build_dir / "generated-app" / "tests.json"
    screenshots_dir = build_dir / "generated-app" / "screenshots"

    # Determine if this is initialization (no tests.json) or continuation
    is_initialization = not tests_json_path.exists()

    if is_enhancement and feature_request_path:
        cmd = [
            "python", "/app/claude_code.py",
            "--enhance-feature", str(feature_request_path),
            "--existing-codebase", str(build_dir / "generated-app"),
            "--model", model,
            "--skip-git-init"
        ]
    elif is_initialization:
        # First run - will create tests.json
        cmd = [
            "python", "/app/claude_code.py",
            "--project", project_name,
            "--model", model,
            "--output-dir", str(build_dir / "generated-app"),
            "--skip-git-init"
        ]
    else:
        # Continuation - don't pass --project to avoid re-initializing
        cmd = [
            "python", "/app/claude_code.py",
            "--model", model,
            "--output-dir", str(build_dir / "generated-app"),
            "--skip-git-init"
        ]

    print(f"Running agent: {' '.join(cmd)}")
    print(f"Working directory: {build_dir}")
    print(f"Authentication: {auth_type}")
    print(f"Mode: {'initialization' if is_initialization else 'continuation'}")

    env = os.environ.copy()

    # Set authentication based on type
    if auth_type == "oauth":
        env['CLAUDE_CODE_OAUTH_TOKEN'] = auth_token
        env.pop('ANTHROPIC_API_KEY', None)
        print("Using Claude subscription (OAuth token)")
    else:
        env['ANTHROPIC_API_KEY'] = auth_token
        env.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
        print("Using Anthropic API key")

    agent_process = subprocess.Popen(
        cmd,
        cwd=str(build_dir),
        env=env
    )

    print(f"Agent started (PID: {agent_process.pid})")

    # Initialize tracking state
    last_progress: dict[int, int] = {}
    feature_issues_created = False
    feature_to_issue: dict[str, int] = {}

    # Monitoring loop - runs while agent is active
    last_check = time.time()

    while agent_process.poll() is None:
        time.sleep(10)  # Check every 10 seconds

        # Only do progress updates at specified interval
        if time.time() - last_check < progress_interval:
            continue

        last_check = time.time()

        # Wait for tests.json to exist
        if not tests_json_path.exists():
            print("Waiting for tests.json to be created...")
            continue

        try:
            tests = json.loads(tests_json_path.read_text())
        except Exception as e:
            print(f"Error reading tests.json: {e}")
            continue

        # Create feature issues on first run (after tests.json is created)
        if not feature_issues_created and tests:
            print("Creating feature issues from tests.json...")
            features = extract_features_from_tests(tests_json_path)

            if features:
                feature_to_issue = create_feature_issues(
                    github_repo=github_repo,
                    github_token=github_token,
                    features=features
                )

                if feature_to_issue:
                    assign_issue_numbers_to_tests(tests_json_path, feature_to_issue)
                    # Reload tests with issue numbers
                    tests = json.loads(tests_json_path.read_text())

            feature_issues_created = True
            print(f"Created {len(feature_to_issue)} feature issues")

        # Post progress updates to feature issues
        if tests and feature_to_issue:
            last_progress = post_feature_progress(
                tests=tests,
                github_repo=github_repo,
                github_token=github_token,
                last_progress=last_progress
            )

            # Post screenshots for each feature issue
            for feature_id, issue_num in feature_to_issue.items():
                global uploaded_screenshots
                uploaded_screenshots = post_screenshots_to_issue(
                    screenshots_dir=screenshots_dir,
                    issue_number=issue_num,
                    github_repo=github_repo,
                    github_token=github_token,
                    uploaded_hashes=uploaded_screenshots
                )

    print(f"Agent completed with exit code: {agent_process.returncode}")
    return agent_process.returncode


def run_agent(
    build_dir: Path,
    auth_token: str,
    auth_type: str = "api_key",
    is_enhancement: bool = False,
    feature_request_path: Optional[Path] = None
):
    """Run the Claude Code agent (simple mode without monitoring).

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


# =============================================================================
# Product Repository Management
# =============================================================================

def create_product_repo(
    project_name: str,
    github_owner: str,
    github_token: str,
    description: str = ""
) -> Optional[str]:
    """Create a new GitHub repo for the product.

    Args:
        project_name: Name for the new repository
        github_owner: GitHub organization or username
        github_token: GitHub token with repo creation permissions
        description: Optional repository description

    Returns:
        Clone URL if successful, None otherwise
    """
    try:
        from github import Github

        gh = Github(github_token)

        # Try org first, fall back to user
        try:
            owner = gh.get_organization(github_owner)
            print(f"Creating repo in organization: {github_owner}")
        except Exception:
            owner = gh.get_user()
            print(f"Creating repo for user: {owner.login}")

        # Check if repo already exists
        try:
            existing = owner.get_repo(project_name)
            print(f"Repository {github_owner}/{project_name} already exists")
            return existing.clone_url
        except Exception:
            pass

        # Create new repo
        repo = owner.create_repo(
            name=project_name,
            description=description or f"Generated by autonomous agent",
            private=True,
            auto_init=True,
            has_issues=True,
            has_projects=False,
            has_wiki=False,
        )

        # Set up labels for feature tracking
        default_labels = [
            ("mvp-build", "FBCA04", "Initial MVP build from BUILD_PLAN.md"),
            ("agent-building", "1D76DB", "Agent is currently working on this"),
            ("agent-complete", "0E8A16", "Agent has completed this feature"),
        ]

        for label_name, color, label_desc in default_labels:
            try:
                repo.create_label(name=label_name, color=color, description=label_desc)
            except Exception:
                pass  # Label may already exist

        print(f"Created repository: {repo.html_url}")
        return repo.clone_url

    except Exception as e:
        print(f"Error creating product repo: {e}")
        return None


# =============================================================================
# Feature Issue Management
# =============================================================================

def extract_features_from_tests(tests_json_path: Path) -> list[str]:
    """Extract unique feature IDs from generated tests.json.

    Args:
        tests_json_path: Path to tests.json file

    Returns:
        List of unique feature IDs in order they appear
    """
    if not tests_json_path.exists():
        return []

    try:
        tests = json.loads(tests_json_path.read_text())
        features = []
        seen = set()

        for test in tests:
            feature = test.get('feature')
            if feature and feature not in seen:
                features.append(feature)
                seen.add(feature)

        return features
    except Exception as e:
        print(f"Error extracting features from tests: {e}")
        return []


def create_feature_issues(
    github_repo: str,
    github_token: str,
    features: list[str]
) -> dict[str, int]:
    """Create GitHub issues for each feature extracted from tests.

    Args:
        github_repo: Repository in format "owner/repo"
        github_token: GitHub token
        features: List of feature IDs to create issues for

    Returns:
        Dictionary mapping feature_id -> issue_number
    """
    try:
        from github import Github

        gh = Github(github_token)
        repo = gh.get_repo(github_repo)
        feature_to_issue = {}

        for feature_id in features:
            # Check if issue already exists for this feature
            label_name = f'feature:{feature_id}'
            existing = list(repo.get_issues(state='all', labels=[label_name]))
            if existing:
                feature_to_issue[feature_id] = existing[0].number
                print(f"Feature '{feature_id}' already has issue #{existing[0].number}")
                continue

            # Create label for this feature
            try:
                repo.create_label(
                    name=label_name,
                    color='C5DEF5',
                    description=f'Tests for {feature_id} feature'
                )
            except Exception:
                pass  # Label may already exist

            # Create issue with human-readable title
            title = f"MVP: {feature_id.replace('-', ' ').replace('_', ' ').title()}"
            body = f"""## {title}

### Feature ID
`{feature_id}`

### Test Progress
*Progress updates will be posted automatically as tests pass.*

### Screenshots
*Screenshots will be posted as tests are verified.*

---
*Auto-created by agent initialization*
"""
            issue = repo.create_issue(
                title=title,
                body=body,
                labels=['mvp-build', 'agent-building', label_name]
            )
            feature_to_issue[feature_id] = issue.number
            print(f"Created issue #{issue.number} for feature '{feature_id}'")

        return feature_to_issue

    except Exception as e:
        print(f"Error creating feature issues: {e}")
        return {}


def assign_issue_numbers_to_tests(
    tests_json_path: Path,
    feature_to_issue: dict[str, int]
) -> bool:
    """Add issueNumber field to each test based on its feature.

    Args:
        tests_json_path: Path to tests.json file
        feature_to_issue: Mapping of feature_id -> issue_number

    Returns:
        True if successful
    """
    try:
        tests = json.loads(tests_json_path.read_text())

        for test in tests:
            feature = test.get('feature')
            if feature and feature in feature_to_issue:
                test['issueNumber'] = feature_to_issue[feature]

        tests_json_path.write_text(json.dumps(tests, indent=2))
        print(f"Assigned issue numbers to {len(tests)} tests")
        return True

    except Exception as e:
        print(f"Error assigning issue numbers to tests: {e}")
        return False


# =============================================================================
# Progress Tracking
# =============================================================================

def post_feature_progress(
    tests: list[dict],
    github_repo: str,
    github_token: str,
    last_progress: dict[int, int]
) -> dict[int, int]:
    """Post progress updates to feature issues, close when complete.

    Args:
        tests: List of test objects from tests.json
        github_repo: Repository in format "owner/repo"
        github_token: GitHub token
        last_progress: Dict mapping issue_number -> last_passed_count

    Returns:
        Updated last_progress dict
    """
    from collections import defaultdict

    try:
        from github import Github

        # Group tests by issue number
        by_issue = defaultdict(list)
        for test in tests:
            issue_num = test.get('issueNumber')
            if issue_num:
                by_issue[issue_num].append(test)

        gh = Github(github_token)
        repo = gh.get_repo(github_repo)

        for issue_num, feature_tests in by_issue.items():
            passed = sum(1 for t in feature_tests if t.get('passes'))
            total = len(feature_tests)

            # Skip if no change
            if last_progress.get(issue_num) == passed:
                continue

            last_progress[issue_num] = passed
            issue = repo.get_issue(issue_num)

            if passed == total and total > 0:
                # Feature complete!
                issue.create_comment("✅ **All tests passing!** Feature complete.")
                issue.edit(state='closed')

                # Update labels
                current_labels = [l.name for l in issue.labels]
                new_labels = [l for l in current_labels if l != 'agent-building']
                if 'agent-complete' not in new_labels:
                    new_labels.append('agent-complete')
                issue.set_labels(*new_labels)

                print(f"Feature complete! Closed issue #{issue_num}")
            else:
                # Progress update
                pct = (passed / total * 100) if total > 0 else 0
                bar_filled = int(pct / 5)
                bar = '█' * bar_filled + '░' * (20 - bar_filled)

                timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
                comment = f"**Progress Update** ({timestamp})\n\n"
                comment += f"`[{bar}]` {passed}/{total} tests ({pct:.0f}%)"

                issue.create_comment(comment)
                print(f"Posted progress to issue #{issue_num}: {passed}/{total}")

        return last_progress

    except Exception as e:
        print(f"Error posting feature progress: {e}")
        return last_progress


def post_screenshots_to_issue(
    screenshots_dir: Path,
    issue_number: int,
    github_repo: str,
    github_token: str,
    uploaded_hashes: set
) -> set:
    """Post new screenshots to GitHub issue.

    Args:
        screenshots_dir: Directory containing screenshots
        issue_number: GitHub issue number
        github_repo: Repository in format "owner/repo"
        github_token: GitHub token
        uploaded_hashes: Set of already-uploaded screenshot hashes

    Returns:
        Updated set of uploaded hashes
    """
    if not screenshots_dir.exists():
        return uploaded_hashes

    try:
        from github import Github

        # Find new screenshots
        new_screenshots = []
        for png in screenshots_dir.glob("**/*.png"):
            content_hash = hashlib.md5(png.read_bytes()).hexdigest()[:8]
            if content_hash not in uploaded_hashes:
                new_screenshots.append((png, content_hash))

        if not new_screenshots:
            return uploaded_hashes

        gh = Github(github_token)
        repo = gh.get_repo(github_repo)
        issue = repo.get_issue(issue_number)

        timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
        comment = f"📸 **Screenshots** ({timestamp})\n\n"

        for png_path, content_hash in new_screenshots[:5]:  # Limit to 5 per comment
            comment += f"**{png_path.name}**\n\n"
            uploaded_hashes.add(content_hash)

        if len(new_screenshots) > 5:
            comment += f"\n*...and {len(new_screenshots) - 5} more screenshots*\n"

        issue.create_comment(comment)
        print(f"Posted {len(new_screenshots)} screenshot(s) to issue #{issue_number}")

        return uploaded_hashes

    except Exception as e:
        print(f"Error posting screenshots: {e}")
        return uploaded_hashes


def get_uploaded_screenshots_from_github(
    github_repo: str,
    issue_number: int,
    github_token: str
) -> set:
    """Query issue comments to find already-uploaded screenshot hashes.

    Used for deduplication when resuming a session.

    Args:
        github_repo: Repository in format "owner/repo"
        issue_number: GitHub issue number
        github_token: GitHub token

    Returns:
        Set of screenshot filenames that were already posted
    """
    try:
        from github import Github
        import re

        gh = Github(github_token)
        repo = gh.get_repo(github_repo)
        issue = repo.get_issue(issue_number)

        uploaded = set()
        for comment in issue.get_comments():
            if "📸 **Screenshots**" in comment.body:
                # Extract screenshot filenames from comment
                matches = re.findall(r'\*\*([^*]+\.png)\*\*', comment.body)
                uploaded.update(matches)

        print(f"Found {len(uploaded)} previously uploaded screenshots")
        return uploaded

    except Exception as e:
        print(f"Error getting uploaded screenshots: {e}")
        return set()


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

        # Run the agent with monitoring (creates feature issues, posts progress)
        exit_code = run_agent_with_monitoring(
            build_dir=build_dir,
            auth_token=auth_token,
            auth_type=auth_type,
            github_repo=github_repo,
            github_token=github_token,
            is_enhancement=is_enhancement,
            feature_request_path=feature_request_path if is_enhancement else None,
            progress_interval=120  # Check progress every 2 minutes
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
