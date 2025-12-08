#!/usr/bin/env python3
"""
VPS Agent Invocation Script via SSH

Replaces AWS boto3 bedrock-agentcore API with SSH commands to VPS.
Connects to VPS and executes docker commands to start/manage agent container.

Usage:
    python ssh_invoke.py \
        --host <VPS_HOST> \
        --user <SSH_USER> \
        --key-file <PATH_TO_SSH_KEY> \
        --session-id <SESSION_ID> \
        --payload <JSON_PAYLOAD>

    # Or with key content directly (for CI environments):
    python ssh_invoke.py \
        --host <VPS_HOST> \
        --user <SSH_USER> \
        --key-content "$VPS_SSH_KEY" \
        --session-id <SESSION_ID> \
        --payload <JSON_PAYLOAD>
"""

import argparse
import json
import os
import sys
import time
from io import StringIO
from typing import Dict, Any, Optional

try:
    import paramiko
except ImportError:
    print("Error: paramiko is required. Install with: pip install paramiko")
    sys.exit(1)


class SSHAgentInvoker:
    """Handles agent invocation via SSH to VPS."""

    def __init__(
        self,
        host: str,
        username: str,
        private_key_path: Optional[str] = None,
        private_key_content: Optional[str] = None,
        port: int = 22,
        max_retries: int = 3,
        container_name: str = "claude-code-agent",
        image_name: str = "claude-code-agent:latest"
    ):
        """Initialize SSH connection parameters.

        Args:
            host: VPS hostname or IP address
            username: SSH username
            private_key_path: Path to SSH private key file
            private_key_content: SSH private key content (for CI)
            port: SSH port (default: 22)
            max_retries: Max connection retry attempts
            container_name: Docker container name
            image_name: Docker image name
        """
        self.host = host
        self.username = username
        self.port = port
        self.max_retries = max_retries
        self.container_name = container_name
        self.image_name = image_name

        # Initialize SSH client
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # Load private key
        self._load_private_key(private_key_path, private_key_content)

    def _load_private_key(
        self,
        key_path: Optional[str],
        key_content: Optional[str]
    ):
        """Load SSH private key from file or content."""
        if key_content:
            # Handle different key formats
            key_content = key_content.strip()

            # Try RSA key first
            try:
                self.pkey = paramiko.RSAKey.from_private_key(StringIO(key_content))
                return
            except Exception:
                pass

            # Try Ed25519
            try:
                self.pkey = paramiko.Ed25519Key.from_private_key(StringIO(key_content))
                return
            except Exception:
                pass

            # Try ECDSA
            try:
                self.pkey = paramiko.ECDSAKey.from_private_key(StringIO(key_content))
                return
            except Exception:
                pass

            raise ValueError("Could not parse SSH private key (tried RSA, Ed25519, ECDSA)")

        elif key_path:
            key_path = os.path.expanduser(key_path)
            if not os.path.exists(key_path):
                raise FileNotFoundError(f"SSH key file not found: {key_path}")

            # Try different key types
            for key_class in [paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey]:
                try:
                    self.pkey = key_class.from_private_key_file(key_path)
                    return
                except Exception:
                    continue

            raise ValueError(f"Could not parse SSH private key file: {key_path}")
        else:
            raise ValueError("Either private_key_path or private_key_content required")

    def connect(self) -> bool:
        """Establish SSH connection with retry logic."""
        for attempt in range(1, self.max_retries + 1):
            try:
                print(f"Connecting to {self.username}@{self.host}:{self.port} (attempt {attempt}/{self.max_retries})...")

                self.client.connect(
                    hostname=self.host,
                    port=self.port,
                    username=self.username,
                    pkey=self.pkey,
                    timeout=30,
                    banner_timeout=30
                )

                print(f"Connected successfully")
                return True

            except paramiko.AuthenticationException as e:
                print(f"Authentication failed: {e}")
                return False

            except Exception as e:
                print(f"Connection error (attempt {attempt}): {e}")
                if attempt < self.max_retries:
                    delay = 5 * attempt
                    print(f"Retrying in {delay} seconds...")
                    time.sleep(delay)

        print("Max connection retries exceeded")
        return False

    def exec_command(self, command: str, timeout: int = 60) -> tuple[int, str, str]:
        """Execute command on VPS and return exit code, stdout, stderr."""
        try:
            stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            stdout_str = stdout.read().decode('utf-8').strip()
            stderr_str = stderr.read().decode('utf-8').strip()
            return exit_code, stdout_str, stderr_str
        except Exception as e:
            return -1, "", str(e)

    def invoke(self, payload: Dict[str, Any], session_id: str) -> bool:
        """
        Invoke agent container on VPS with payload.

        - Stops any existing container
        - Starts new container with payload as environment variable
        - Returns after container starts (async execution)

        Args:
            payload: JSON payload with issue details
            session_id: Unique session identifier

        Returns:
            True if container started successfully
        """
        print()
        print("=" * 80)
        print("VPS Agent Invocation")
        print("=" * 80)
        print(f"Host: {self.host}")
        print(f"Session ID: {session_id}")
        print(f"Payload: {json.dumps(payload, indent=2)}")
        print("=" * 80)
        print()

        # Connect to VPS
        if not self.connect():
            return False

        try:
            # Check for existing container
            print("Checking for existing container...")
            exit_code, stdout, _ = self.exec_command(
                f"docker inspect {self.container_name} --format '{{{{.State.Status}}}}' 2>/dev/null || echo 'not_found'"
            )
            current_status = stdout.strip()
            print(f"Current container status: {current_status}")

            # Stop existing container if running
            if current_status in ['running', 'paused', 'restarting']:
                print(f"Stopping existing container...")
                self.exec_command(f"docker stop {self.container_name}", timeout=120)

            # Remove existing container
            if current_status != 'not_found':
                print("Removing existing container...")
                self.exec_command(f"docker rm {self.container_name}")

            # Prepare payload (escape for shell)
            payload_json = json.dumps(payload)
            # Use base64 encoding to safely pass JSON through shell
            import base64
            payload_b64 = base64.b64encode(payload_json.encode()).decode()

            # Build docker run command
            docker_cmd = f'''docker run -d \\
                --name {self.container_name} \\
                --restart unless-stopped \\
                -v /opt/agent/data:/app/workspace \\
                -v /opt/agent/metrics:/app/metrics \\
                -v /opt/agent/previews:/app/previews \\
                -v /opt/agent/secrets:/app/secrets:ro \\
                -e SESSION_ID="{session_id}" \\
                -e AGENT_PAYLOAD="$(echo '{payload_b64}' | base64 -d)" \\
                -e GITHUB_TOKEN_FILE=/app/secrets/github_token \\
                -e ANTHROPIC_API_KEY_FILE=/app/secrets/anthropic_api_key \\
                -e METRICS_FILE=/app/metrics/health.json \\
                {self.image_name} \\
                python vps_entrypoint.py'''

            print("Starting agent container...")
            print(f"Command: docker run -d --name {self.container_name} ...")

            exit_code, stdout, stderr = self.exec_command(docker_cmd, timeout=60)

            if exit_code == 0 and stdout:
                container_id = stdout[:12]
                print(f"Container started: {container_id}")

                # Verify container is running
                time.sleep(2)
                exit_code, status, _ = self.exec_command(
                    f"docker inspect {self.container_name} --format '{{{{.State.Status}}}}'"
                )

                if status == 'running':
                    print(f"Container is running")

                    # Show initial logs
                    _, logs, _ = self.exec_command(
                        f"docker logs {self.container_name} --tail 20 2>&1"
                    )
                    if logs:
                        print()
                        print("Initial container logs:")
                        print("-" * 40)
                        print(logs)
                        print("-" * 40)

                    return True
                else:
                    print(f"Container status: {status}")
                    _, logs, _ = self.exec_command(
                        f"docker logs {self.container_name} 2>&1"
                    )
                    print(f"Container logs:\n{logs}")
                    return False
            else:
                print(f"Failed to start container")
                print(f"Exit code: {exit_code}")
                print(f"Stdout: {stdout}")
                print(f"Stderr: {stderr}")
                return False

        except Exception as e:
            print(f"Error: {e}")
            return False

        finally:
            self.client.close()

    def check_status(self) -> Dict[str, Any]:
        """Check container status and return health info."""
        if not self.connect():
            return {"status": "connection_failed"}

        try:
            # Get container status
            exit_code, status, _ = self.exec_command(
                f"docker inspect {self.container_name} --format '{{{{.State.Status}}}}' 2>/dev/null || echo 'not_found'"
            )

            if status == 'not_found':
                return {"status": "not_running"}

            # Get metrics if available
            _, metrics_json, _ = self.exec_command(
                "cat /opt/agent/metrics/health.json 2>/dev/null || echo '{}'"
            )

            try:
                metrics = json.loads(metrics_json)
            except:
                metrics = {}

            return {
                "status": status,
                "metrics": metrics
            }

        finally:
            self.client.close()

    def stop(self) -> bool:
        """Stop the agent container."""
        if not self.connect():
            return False

        try:
            print(f"Stopping container {self.container_name}...")
            exit_code, _, _ = self.exec_command(
                f"docker stop {self.container_name} && docker rm {self.container_name}"
            )
            return exit_code == 0
        finally:
            self.client.close()


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Invoke VPS agent via SSH'
    )

    parser.add_argument(
        '--host',
        required=True,
        help='VPS hostname or IP address'
    )

    parser.add_argument(
        '--user',
        required=True,
        help='SSH username'
    )

    parser.add_argument(
        '--key-file',
        help='Path to SSH private key file'
    )

    parser.add_argument(
        '--key-content',
        help='SSH private key content (for CI environments)'
    )

    parser.add_argument(
        '--port',
        type=int,
        default=22,
        help='SSH port (default: 22)'
    )

    parser.add_argument(
        '--session-id',
        required=True,
        help='Unique session identifier'
    )

    parser.add_argument(
        '--payload',
        required=True,
        help='JSON payload for the agent'
    )

    parser.add_argument(
        '--container-name',
        default='claude-code-agent',
        help='Docker container name (default: claude-code-agent)'
    )

    parser.add_argument(
        '--image-name',
        default='claude-code-agent:latest',
        help='Docker image name (default: claude-code-agent:latest)'
    )

    parser.add_argument(
        '--max-retries',
        type=int,
        default=3,
        help='Max connection retry attempts (default: 3)'
    )

    parser.add_argument(
        '--action',
        choices=['invoke', 'status', 'stop'],
        default='invoke',
        help='Action to perform (default: invoke)'
    )

    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()

    # Validate key options
    if not args.key_file and not args.key_content:
        # Try environment variable
        key_content = os.environ.get('VPS_SSH_KEY')
        if key_content:
            args.key_content = key_content
        else:
            print("Error: Either --key-file, --key-content, or VPS_SSH_KEY env var required")
            sys.exit(1)

    # Parse payload
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON payload: {e}")
        sys.exit(1)

    # Create invoker
    invoker = SSHAgentInvoker(
        host=args.host,
        username=args.user,
        private_key_path=args.key_file,
        private_key_content=args.key_content,
        port=args.port,
        max_retries=args.max_retries,
        container_name=args.container_name,
        image_name=args.image_name
    )

    # Perform action
    if args.action == 'invoke':
        success = invoker.invoke(payload, args.session_id)
        if success:
            print()
            print("Agent invocation completed successfully")
            print("The agent is now running in the background on the VPS.")
            print("Monitor progress via the GitHub issue or VPS logs.")
            sys.exit(0)
        else:
            print()
            print("Agent invocation failed")
            sys.exit(1)

    elif args.action == 'status':
        status = invoker.check_status()
        print(json.dumps(status, indent=2))
        sys.exit(0 if status.get('status') == 'running' else 1)

    elif args.action == 'stop':
        success = invoker.stop()
        if success:
            print("Agent stopped successfully")
            sys.exit(0)
        else:
            print("Failed to stop agent")
            sys.exit(1)


if __name__ == '__main__':
    main()
