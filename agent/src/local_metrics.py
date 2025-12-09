"""Local file-based metrics publisher for VPS deployment.

Replaces CloudWatch metrics with a local JSON file that can be read via SSH
by GitHub Actions for health monitoring. Maintains the same interface as
MetricsPublisher from cloudwatch_metrics.py for easy swap.
"""

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class LocalMetricsPublisher:
    """Publishes metrics to a local JSON file for health monitoring.

    The metrics file can be read via SSH by GitHub Actions to check:
    - Session heartbeat (is the agent still alive?)
    - Current issue being worked on
    - Progress metrics (elapsed time, commits, etc.)

    File format:
    {
        "current_issue": 123,
        "session_id": "gh-issue-123-...",
        "status": "running",
        "last_heartbeat": "2025-01-08T12:00:00Z",
        "elapsed_hours": 1.5,
        "total_commits": 5,
        ...
    }
    """

    def __init__(
        self,
        issue_number: Optional[int] = None,
        session_id: Optional[str] = None,
        enabled: bool = True,
        metrics_file: Optional[str] = None,
    ):
        """Initialize the local metrics publisher.

        Args:
            issue_number: GitHub issue number being worked on
            session_id: Session ID for tracking
            enabled: Whether to actually write metrics
            metrics_file: Path to metrics JSON file (default: /app/metrics/health.json
                          or METRICS_FILE env var)
        """
        self.issue_number = issue_number
        self.session_id = session_id
        self.enabled = enabled and os.environ.get(
            "LOCAL_METRICS_ENABLED", "true"
        ).lower() == "true"
        self._total_commits = 0

        # Determine metrics file path
        if metrics_file:
            self.metrics_file = Path(metrics_file)
        else:
            default_path = "/app/metrics/health.json"
            self.metrics_file = Path(os.environ.get("METRICS_FILE", default_path))

        # Ensure directory exists
        if self.enabled:
            self.metrics_file.parent.mkdir(parents=True, exist_ok=True)

    def _read_metrics(self) -> dict:
        """Read current metrics from file."""
        if self.metrics_file.exists():
            try:
                return json.loads(self.metrics_file.read_text())
            except (json.JSONDecodeError, IOError):
                return {}
        return {}

    def _write_metrics(self, metrics: dict) -> bool:
        """Write metrics to file with file locking for safe concurrent access.

        Returns:
            True if successful, False otherwise
        """
        if not self.enabled:
            return False

        try:
            # Ensure parent directory exists
            self.metrics_file.parent.mkdir(parents=True, exist_ok=True)

            # Write with exclusive lock to prevent corruption
            with open(self.metrics_file, 'w') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump(metrics, f, indent=2)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return True
        except Exception as e:
            print(f"Warning: Failed to write metrics: {e}")
            return False

    def _update_metrics(self, **kwargs) -> bool:
        """Update specific fields in the metrics file.

        Args:
            **kwargs: Fields to update in the metrics dict

        Returns:
            True if successful, False otherwise
        """
        metrics = self._read_metrics()
        metrics.update(kwargs)
        metrics["last_updated"] = datetime.now(timezone.utc).isoformat()
        return self._write_metrics(metrics)

    # === Session Lifecycle Metrics ===

    def publish_session_started(self, mode: str = "full_build") -> bool:
        """Record session start.

        Args:
            mode: Either 'full_build' or 'enhancement'
        """
        metrics = {
            "current_issue": self.issue_number,
            "session_id": self.session_id,
            "status": "running",
            "mode": mode,
            "session_started": datetime.now(timezone.utc).isoformat(),
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "total_commits": 0,
            "elapsed_hours": 0,
            "cost_usd": 0,
            "api_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        self._total_commits = 0
        return self._write_metrics(metrics)

    def publish_session_completed(self, exit_code: int, duration_seconds: float) -> bool:
        """Record session completion with exit code and duration."""
        return self._update_metrics(
            status="completed",
            exit_code=exit_code,
            duration_seconds=duration_seconds,
            session_completed=datetime.now(timezone.utc).isoformat(),
        )

    def publish_session_heartbeat(self) -> bool:
        """Update session heartbeat timestamp.

        This is the primary method used by GitHub Actions to check if
        the agent is still alive. It reads the last_heartbeat field
        and compares against a staleness threshold (default 300s).
        """
        return self._update_metrics(
            last_heartbeat=datetime.now(timezone.utc).isoformat(),
            status="running",
        )

    # === Progress Metrics ===

    def publish_progress(
        self,
        elapsed_hours: float,
        remaining_hours: float,
        cost_usd: float = 0.0,
        api_calls: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> bool:
        """Publish comprehensive progress metrics.

        This is the main method called every 30 seconds during agent execution.
        """
        return self._update_metrics(
            elapsed_hours=elapsed_hours,
            remaining_hours=remaining_hours,
            cost_usd=cost_usd,
            api_calls=api_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_commits=self._total_commits,
            last_heartbeat=datetime.now(timezone.utc).isoformat(),
        )

    # === Git/GitHub Metrics ===

    def publish_commits_pushed(self, count: int) -> bool:
        """Record successful commit push.

        Args:
            count: Number of commits pushed in this push operation
        """
        self._total_commits += count
        return self._update_metrics(
            total_commits=self._total_commits,
            last_push=datetime.now(timezone.utc).isoformat(),
            last_push_count=count,
        )

    def publish_push_failed(self) -> bool:
        """Record failed push event."""
        return self._update_metrics(
            last_push_failed=datetime.now(timezone.utc).isoformat(),
        )

    def publish_screenshots_uploaded(self, count: int) -> bool:
        """Record screenshot upload count."""
        metrics = self._read_metrics()
        total_screenshots = metrics.get("total_screenshots", 0) + count
        return self._update_metrics(
            total_screenshots=total_screenshots,
            last_screenshot_upload=datetime.now(timezone.utc).isoformat(),
        )

    # === Error Metrics ===

    def publish_error(self, error_type: str) -> bool:
        """Record error event with type.

        Args:
            error_type: Type of error (e.g., 'setup_failed', 'push_failed', 'agent_crash')
        """
        metrics = self._read_metrics()
        errors = metrics.get("errors", [])
        errors.append({
            "type": error_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        # Keep only last 50 errors
        errors = errors[-50:]
        return self._update_metrics(
            errors=errors,
            last_error=error_type,
            last_error_time=datetime.now(timezone.utc).isoformat(),
            status="error",
        )

    # === Additional VPS-specific methods ===

    def clear(self) -> bool:
        """Clear all metrics (on session end or cleanup)."""
        return self._write_metrics({})

    def get_status(self) -> dict:
        """Get current metrics status (for debugging/inspection)."""
        return self._read_metrics()
