#!/usr/bin/env python3
"""
Test Generation Script for Spec Bot

Generates tests from an approved XML specification and inserts them
into the VPS tests.json file via SSH.

Usage:
    python generate_tests.py \
        --issue-number 42 \
        --repo owner/repo

Environment variables:
    GITHUB_TOKEN: GitHub API token
    ANTHROPIC_API_KEY: Anthropic API key for test generation
    VPS_HOST: VPS hostname or IP
    VPS_SSH_USER: SSH username
    VPS_SSH_KEY: SSH private key content
"""

import argparse
import json
import os
import re
import sys
from io import StringIO
from typing import List, Dict, Any, Optional

try:
    import anthropic
except ImportError:
    print("Error: anthropic package required. Install with: pip install anthropic")
    sys.exit(1)

try:
    import paramiko
except ImportError:
    print("Error: paramiko package required. Install with: pip install paramiko")
    sys.exit(1)


def get_spec_from_issue(issue_number: int, repo: str, github_token: str) -> Optional[str]:
    """Extract the latest XML spec from issue comments."""
    import urllib.request
    import urllib.error

    # Get issue comments via GitHub API
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            comments = json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        print(f"Error fetching comments: {e}")
        return None

    # Find the latest bot comment with XML spec
    latest_spec = None
    for comment in comments:
        if comment.get("user", {}).get("login") == "github-actions[bot]":
            body = comment.get("body", "")
            # Extract XML from code block
            match = re.search(r"```xml\n(.*?)```", body, re.DOTALL)
            if match:
                latest_spec = match.group(1).strip()

    return latest_spec


def extract_feature_name(spec: str) -> str:
    """Extract feature name from XML spec."""
    match = re.search(r"<feature_name>\s*(.*?)\s*</feature_name>", spec, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Fallback: generate from first few words
    return "user-feature"


def generate_tests_from_spec(spec: str, feature_name: str, issue_number: int, api_key: str) -> List[Dict]:
    """Use Claude to generate tests from the XML specification."""
    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""You are generating tests for an autonomous coding agent.

Given this feature specification, generate 5-15 detailed tests that verify the feature works correctly.

SPECIFICATION:
{spec}

REQUIREMENTS:
1. Each test must have these exact fields:
   - "feature": "{feature_name}" (use this exact value)
   - "issueNumber": {issue_number} (use this exact number)
   - "category": "functional" or "style"
   - "description": A clear description of what the test verifies
   - "steps": An array of detailed steps to execute and verify
   - "passes": false (always false initially)

2. Include both functional tests (behavior) and style tests (UI appearance)
3. Steps should be specific and actionable
4. Cover edge cases and error states
5. Tests should be ordered from basic to complex

OUTPUT FORMAT:
Return ONLY a valid JSON array of test objects. No other text.

Example format:
[
  {{
    "feature": "{feature_name}",
    "issueNumber": {issue_number},
    "category": "functional",
    "description": "Feature toggle enables the setting",
    "steps": [
      "Navigate to settings page",
      "Locate the feature toggle",
      "Click to enable the feature",
      "Verify toggle shows enabled state",
      "Verify feature is active in the UI"
    ],
    "passes": false
  }}
]"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )

    # Extract JSON from response
    response_text = response.content[0].text.strip()

    # Try to parse as JSON
    try:
        tests = json.loads(response_text)
        return tests
    except json.JSONDecodeError:
        # Try to extract JSON array from response
        match = re.search(r"\[.*\]", response_text, re.DOTALL)
        if match:
            try:
                tests = json.loads(match.group())
                return tests
            except json.JSONDecodeError:
                pass

    print(f"Failed to parse tests from response: {response_text[:500]}")
    return []


def connect_ssh(host: str, user: str, key_content: str) -> paramiko.SSHClient:
    """Establish SSH connection to VPS."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    # Try different key types
    pkey = None
    key_content = key_content.strip()

    for key_class in [paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey]:
        try:
            pkey = key_class.from_private_key(StringIO(key_content))
            break
        except Exception:
            continue

    if not pkey:
        raise ValueError("Could not parse SSH key")

    client.connect(
        hostname=host,
        username=user,
        pkey=pkey,
        timeout=30
    )

    return client


def prepend_tests_to_vps(
    tests: List[Dict],
    host: str,
    user: str,
    key_content: str,
    tests_path: str = "/opt/agent/data/tests.json"
) -> bool:
    """SSH to VPS and prepend tests to tests.json."""
    try:
        client = connect_ssh(host, user, key_content)

        # Read existing tests
        stdin, stdout, stderr = client.exec_command(f"cat {tests_path} 2>/dev/null || echo '[]'")
        existing_json = stdout.read().decode().strip()

        try:
            existing_tests = json.loads(existing_json)
        except json.JSONDecodeError:
            existing_tests = []

        # Prepend new tests (they go to the top = highest priority)
        combined_tests = tests + existing_tests

        # Write back
        combined_json = json.dumps(combined_tests, indent=2)
        # Escape for shell
        escaped_json = combined_json.replace("'", "'\"'\"'")

        cmd = f"echo '{escaped_json}' > {tests_path}"
        stdin, stdout, stderr = client.exec_command(cmd)
        exit_code = stdout.channel.recv_exit_status()

        client.close()

        if exit_code != 0:
            print(f"Failed to write tests: {stderr.read().decode()}")
            return False

        return True

    except Exception as e:
        print(f"SSH error: {e}")
        return False


def get_queue_position(
    host: str,
    user: str,
    key_content: str,
    issue_number: int,
    tests_path: str = "/opt/agent/data/tests.json"
) -> int:
    """Get the queue position for tests of this issue."""
    try:
        client = connect_ssh(host, user, key_content)

        stdin, stdout, stderr = client.exec_command(f"cat {tests_path}")
        tests_json = stdout.read().decode()
        client.close()

        tests = json.loads(tests_json)

        # Find first test for this issue
        for i, test in enumerate(tests):
            if test.get("issueNumber") == issue_number:
                return i + 1

        return -1

    except Exception as e:
        print(f"Error getting queue position: {e}")
        return -1


def post_comment(issue_number: int, repo: str, github_token: str, body: str):
    """Post a comment to the GitHub issue."""
    import urllib.request
    import urllib.error

    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }

    data = json.dumps({"body": body}).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req) as response:
            return response.status == 201
    except urllib.error.HTTPError as e:
        print(f"Error posting comment: {e}")
        return False


def add_label(issue_number: int, repo: str, github_token: str, label: str):
    """Add a label to the GitHub issue."""
    import urllib.request
    import urllib.error

    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/labels"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }

    data = json.dumps({"labels": [label]}).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req) as response:
            return response.status == 200
    except urllib.error.HTTPError as e:
        print(f"Error adding label: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Generate tests from spec and queue on VPS")
    parser.add_argument("--issue-number", type=int, required=True, help="GitHub issue number")
    parser.add_argument("--repo", required=True, help="GitHub repository (owner/repo)")
    args = parser.parse_args()

    # Get environment variables
    github_token = os.environ.get("GITHUB_TOKEN")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    vps_host = os.environ.get("VPS_HOST")
    vps_user = os.environ.get("VPS_SSH_USER")
    vps_key = os.environ.get("VPS_SSH_KEY")

    if not all([github_token, anthropic_key, vps_host, vps_user, vps_key]):
        print("Missing required environment variables")
        sys.exit(1)

    print(f"Processing issue #{args.issue_number} in {args.repo}")

    # 1. Get the spec from issue comments
    spec = get_spec_from_issue(args.issue_number, args.repo, github_token)
    if not spec:
        print("No spec found in issue comments")
        post_comment(
            args.issue_number, args.repo, github_token,
            "Could not find a specification to generate tests from. Please ensure the spec draft comment exists."
        )
        sys.exit(1)

    print(f"Found spec:\n{spec[:200]}...")

    # 2. Extract feature name
    feature_name = extract_feature_name(spec)
    print(f"Feature name: {feature_name}")

    # 3. Generate tests
    print("Generating tests...")
    tests = generate_tests_from_spec(spec, feature_name, args.issue_number, anthropic_key)

    if not tests:
        print("Failed to generate tests")
        post_comment(
            args.issue_number, args.repo, github_token,
            "Failed to generate tests from the specification. Please try again."
        )
        sys.exit(1)

    print(f"Generated {len(tests)} tests")

    # 4. Prepend tests to VPS
    print("Uploading tests to VPS...")
    success = prepend_tests_to_vps(tests, vps_host, vps_user, vps_key)

    if not success:
        print("Failed to upload tests")
        post_comment(
            args.issue_number, args.repo, github_token,
            "Failed to upload tests to the agent. Please try again or contact support."
        )
        sys.exit(1)

    # 5. Get queue position
    position = get_queue_position(vps_host, vps_user, vps_key, args.issue_number)

    # 6. Post success comment
    test_list = "\n".join([f"- {t['description']}" for t in tests[:5]])
    if len(tests) > 5:
        test_list += f"\n- ... and {len(tests) - 5} more"

    comment = f"""## Specification Approved

Your feature has been approved and {len(tests)} tests have been generated:

{test_list}

### Queue Status

**Position in queue: #{position}**

The agent will work on your feature based on queue position. Progress updates will be posted here as tests pass.

### Labels
- `spec-approved` - Specification finalized
- `agent-building` - Queued for agent implementation
"""

    post_comment(args.issue_number, args.repo, github_token, comment)

    # 7. Update labels
    add_label(args.issue_number, args.repo, github_token, "spec-approved")
    add_label(args.issue_number, args.repo, github_token, "agent-building")

    print(f"Success! {len(tests)} tests queued at position #{position}")


if __name__ == "__main__":
    main()
