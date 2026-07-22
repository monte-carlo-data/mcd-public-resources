"""Matillion Data Productivity Cloud (DPC / "Maia") ETL connector.

The connector is scoped to a single Matillion **project** (supplied as the
``project_id`` credential) — that project *is* the Monte Carlo connection, so
multiple projects mean multiple integrations. This mirrors DPC's own per-project
role-based access and lets the two hierarchies map 1:1 with no collapsing:

- **project** → the connection/integration scope (from ``project_id``)
- **group**   → Matillion Environment (carried on each schedule and execution)
- **job**     → a published Pipeline, keyed by its ``pipelineName`` — the id
  Matillion exposes to SQL for query tagging (it does *not* expose the schedule
  id, so the pipeline id is what lineage tags can reference)
- **task**    → pipeline Component / step, emitted at runtime as ``task_runs``
  on run events (``.../pipeline-executions/{id}/steps``)

Jobs are discovered by listing the project's **schedules**
(``GET /v1/projects/{id}/schedules``): each schedule carries its pipeline,
environment, and cron in one paginated call, so ``(limit, offset)`` maps
straight onto the endpoint's ``page``/``size``. A pipeline with several
schedules yields the same ``job_source_id`` more than once (idempotent — the
backend upserts by ``job_source_id``); a pipeline with no schedule is not
discovered.

An environment is a warehouse-connection config, so the same pipeline scheduled
in ``dev`` vs ``prod`` runs against different warehouses. Modeling environment
as the group keeps those runs in distinct groups.

Auth is OAuth2 client-credentials against ``id.core.matillion.com``; the
resulting bearer token is short-lived (~30 min) and re-requested on expiry.

There is no official Matillion Python SDK, so this connector talks to the
REST API directly via ``requests``.

Components/tasks & lineage:

- The API exposes a pipeline's components only at runtime (a run's ``/steps``),
  not on any structural listing. ``fetch_metadata`` therefore enriches each job
  with its components as declarative ``tasks`` best-effort, from the pipeline's
  most recent run (see ``_schedule_tasks``); the same components also appear at
  runtime as ``task_runs`` on run events. No job→job sub-pipeline lineage is
  emitted.

- **Table ↔ pipeline (warehouse)** lineage is NOT available from the DPC API
  — it exposes no structured read/written-table information (table names
  appear only inside free-text component ``message`` strings, which are too
  inconsistent to parse reliably). ``inputs``/``outputs`` are therefore
  intentionally omitted. To get warehouse lineage, use **SQL query tagging**
  instead: tag each pipeline's SQL with a JSON comment carrying ``mcd_job_id``
  set to this connector's ``job_source_id`` — i.e. the ``pipelineName`` — e.g.::

      -- {"mcd_job_id": "my_pipeline.orch.yaml"}
      CREATE TABLE ... AS SELECT ...

  Optionally add ``mcd_task_id`` (a component's task
  name, i.e. its ``task_source_id``) to attribute lineage to a specific
  component, and
  ``mcd_resource_id`` (the ETL connection's resource UUID, assigned by Monte
  Carlo after the integration is registered) to disambiguate when multiple
  connections share pipeline names. Monte Carlo ingests these tags through its
  standard Snowflake query-log collection and resolves them back to the jobs/
  tasks this connector reports — no ``inputs``/``outputs`` and no extra
  connector code required. See the repo README "lineage via SQL query tagging".
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import List, Optional

import requests

# OAuth2 client-credentials token endpoint (region-independent).
TOKEN_URL = "https://id.core.matillion.com/oauth/dpc/token"
TOKEN_AUDIENCE = "https://api.matillion.com"

# Base URL for a pipeline execution in the Matillion observability dashboard.
# The full run link is this + the pipeline execution id.
_RUN_URL_PREFIX = "https://app.matillion.com/observability-dashboard/pipeline/"

# Refresh the token this many seconds before its stated expiry.
_TOKEN_EXPIRY_SKEW_SECONDS = 60

# Cursor page size when walking a run window (server caps the effective size).
_EXECUTIONS_PAGE_SIZE = 500
# Component steps per run — a single pipeline rarely has more than a handful.
_STEPS_PAGE_SIZE = 100

# Raw Matillion statuses that represent a finished run (require ``end_time``).
_TERMINAL_VENDOR_STATUSES = frozenset(
    {"SUCCESS", "FAILED", "CANCELLED", "SKIPPED", "FORBIDDEN"}
)
# Raw Matillion statuses that represent a failure (require an ``error`` dict).
_ERROR_VENDOR_STATUSES = frozenset({"FAILED", "FORBIDDEN"})

# Matillion trigger → Monte Carlo ETL_RUN_TRIGGER_VALUES. Unmapped → omitted.
_TRIGGER_MAP = {
    "DESIGNER": "MANUAL",
    "API": "API",
    "SCHEDULE": "SCHEDULE",
    "SCHEDULE_RUN_NOW": "MANUAL",
}


class Connector:
    """ETL connector for Matillion Data Productivity Cloud."""

    credentials: dict

    ########################################
    # Connection Related Methods
    ########################################

    def setup_connection(self) -> None:
        """Initialize the HTTP session and validate credentials by fetching a token.

        Reads from ``self.credentials`` (``connect_args`` in credentials.json):

        - ``client_id`` (required) — DPC API credential id
        - ``client_secret`` (required) — DPC API credential secret
        - ``project_id`` (required) — UUID of the DPC project this connection
          collects; the integration is scoped to exactly one project
        - ``region`` (optional, default ``"us1"``) — DPC region (``us1`` or ``eu1``)
        - ``api_base_url`` (optional) — override the derived region base URL
        """
        self._client_id = self.credentials["client_id"]
        self._client_secret = self.credentials["client_secret"]
        self._project_id = self.credentials["project_id"]
        region = self.credentials.get("region", "us1")
        self._base_url = (
            self.credentials.get("api_base_url")
            or f"https://{region}.api.matillion.com/dpc"
        ).rstrip("/")

        self._session = requests.Session()
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

        # Fail fast if credentials are wrong
        self._ensure_token()

    def close_connection(self) -> None:
        """Close the HTTP session."""
        session = getattr(self, "_session", None)
        if session is not None:
            session.close()

    ########################################
    # Auth + HTTP helpers
    ########################################

    def _ensure_token(self) -> None:
        """Fetch (or refresh) the bearer token when missing or near expiry."""
        if self._token and time.monotonic() < self._token_expires_at:
            return
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "audience": TOKEN_AUDIENCE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 1800))
        self._token_expires_at = (
            time.monotonic() + expires_in - _TOKEN_EXPIRY_SKEW_SECONDS
        )

    def _request(self, path: str, params: Optional[dict] = None) -> dict:
        """Perform a GET against the DPC API, refreshing the token on 401."""
        url = f"{self._base_url}{path}"
        for attempt in range(2):
            self._ensure_token()
            resp = self._session.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=60,
            )
            if resp.status_code == 401 and attempt == 0:
                # Token may have expired early — force a refresh and retry once.
                self._token = None
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(
            "Unreachable: request retry loop exhausted"
        )  # pragma: no cover

    ########################################
    # Metadata Fetching
    ########################################

    def fetch_metadata(self, limit: int, offset: int) -> List[dict]:
        """Return a page of pipeline (job) assets, one per schedule.

        See the module docstring for the schedule→pipeline job model. The
        framework's ``(limit, offset)`` maps onto the ``/schedules`` endpoint's
        ``page``/``size``, and each asset is enriched best-effort with its
        pipeline's components as ``tasks`` (see :meth:`_schedule_tasks`).
        """
        body = self._request(
            f"/v1/projects/{self._project_id}/schedules",
            params={"page": offset // limit, "size": limit},
        )
        assets: List[dict] = []
        for schedule in body.get("results", []) or []:
            # A pipeline with multiple schedules emits its asset once per
            # schedule; the backend upserts by job_source_id, so the last-seen
            # schedule's cron wins. Enrichment costs two requests per schedule
            # (latest run + its steps) on every metadata page, and refetches a
            # pipeline's steps once per schedule — fine for typical per-project
            # schedule counts.
            asset = self._build_asset(schedule)
            tasks = self._schedule_tasks(schedule["scheduleId"])
            if tasks:
                asset["tasks"] = tasks
            assets.append(asset)
        return assets

    def _build_asset(self, schedule: dict) -> dict:
        """Build an EtlAsset dict from a schedule (keyed by its pipeline)."""
        pipeline_name = schedule["pipelineName"]
        return {
            "job_source_id": pipeline_name,
            "name": _pipeline_display_name(pipeline_name),
            "group": _group(schedule["environmentName"]),
            "schedule": _compact(
                {
                    "kind": "cron",
                    "cron_expression": schedule.get("cronExpression"),
                    "timezone": schedule.get("cronTimezone"),
                    "next_run_at": _to_iso(schedule.get("nextRunAt")),
                    "paused": not schedule.get("scheduleEnabled", True),
                }
            ),
        }

    def _schedule_tasks(self, schedule_id: str) -> List[dict]:
        """Best-effort component list for a schedule's pipeline, as ``tasks``.

        Components only surface at runtime, so read the schedule's most recent
        execution (``scheduleId``-filtered, newest first) and turn its ``/steps``
        into tasks keyed by component name — the same ``task_source_id`` the
        runtime ``task_runs`` use. A schedule that has never run yields ``[]``.
        """
        runs = (
            self._request(
                "/v1/pipeline-executions",
                {"projectId": self._project_id, "scheduleId": schedule_id, "limit": 1},
            ).get("results")
            or []
        )
        if not runs:
            return []
        run_id = runs[0].get("pipelineExecutionId")
        steps = (
            self._request(
                f"/v1/projects/{self._project_id}/pipeline-executions/{run_id}/steps",
                {"size": _STEPS_PAGE_SIZE},
            ).get("results")
            or []
        )
        tasks: List[dict] = []
        seen: set[str] = set()
        for step in steps:
            task_id = _step_task_id(step)
            if task_id and task_id not in seen:
                seen.add(task_id)
                tasks.append({"task_source_id": task_id, "name": task_id})
        return tasks

    ########################################
    # Run Detail Fetching
    ########################################

    def fetch_run_details(
        self,
        run_ids: Optional[List[str]] = None,
        window_start: Optional[datetime] = None,
        window_end: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        """Fetch run events in polling mode (time window) or webhook mode (run_ids)."""
        if run_ids is None and (window_start is None or window_end is None):
            raise ValueError(
                "Provide run_ids (webhook mode) or both window_start and "
                "window_end (polling mode)"
            )

        if run_ids is not None:
            return self._fetch_runs_by_id(run_ids)

        assert window_start is not None and window_end is not None
        executions = self._list_executions_in_window(window_start, window_end)
        page = executions[offset : offset + limit]
        events = (self._build_run_event(execution) for execution in page)
        return [event for event in events if event is not None]

    def _list_executions_in_window(
        self, window_start: datetime, window_end: datetime
    ) -> List[dict]:
        """List all executions started within ``[window_start, window_end)``.

        Scoped to the configured project. Follows the cursor
        (``paginationToken`` / ``more``) internally and filters client-side
        against the pinned bounds — closed lower, open upper — so pagination
        via ``offset`` is stable and never skips or duplicates runs.

        Every execution carries a ``pipelineName`` (the job), so runs of any
        trigger — scheduled or ad-hoc/manual — are reported.
        """
        params = {
            "startedAfter": _to_api_datetime(window_start),
            "startedBefore": _to_api_datetime(window_end),
            "projectId": self._project_id,
            "limit": _EXECUTIONS_PAGE_SIZE,
        }
        executions: List[dict] = []
        seen_tokens: set[str] = set()
        while True:
            body = self._request("/v1/pipeline-executions", params)
            executions.extend(body.get("results", []) or [])
            token = body.get("more")
            if not token or token in seen_tokens:
                break
            seen_tokens.add(token)
            params["paginationToken"] = token

        filtered = []
        for execution in executions:
            started = _parse_datetime(execution.get("startedAt"))
            if started is None:
                continue
            if window_start <= started < window_end and execution.get("pipelineName"):
                filtered.append(execution)

        filtered.sort(
            key=lambda e: (e.get("startedAt") or "", e.get("pipelineExecutionId") or "")
        )
        return filtered

    def _fetch_runs_by_id(self, run_ids: List[str]) -> List[dict]:
        """Webhook mode: resolve each execution id within the configured project."""
        events: List[dict] = []
        for run_id in run_ids:
            try:
                body = self._request(
                    f"/v1/projects/{self._project_id}/pipeline-executions/{run_id}"
                )
            except requests.HTTPError:
                continue
            # The per-run detail endpoint wraps the execution in ``{"result": …}``
            # and omits projectId — unwrap and restore it for id/group building.
            inner = body.get("result")
            execution = inner if isinstance(inner, dict) else body
            execution.setdefault("projectId", self._project_id)
            event = self._build_run_event(execution)
            if event is not None:
                events.append(event)
        return events

    def _build_run_event(self, execution: dict) -> Optional[dict]:
        """Convert a Matillion execution into an EtlRunEvent dict.

        A job is a pipeline, so the run's ``job_source_id`` is its
        ``pipelineName``. Executions without one can't attribute to a job and
        return ``None`` for the caller to drop.
        """
        pipeline_name = execution.get("pipelineName")
        if not pipeline_name:
            return None

        project_id = execution.get("projectId")
        run_id = execution.get("pipelineExecutionId")
        raw_status = execution.get("status") or "UNKNOWN"

        started_at = _to_iso(execution.get("startedAt"))
        finished_at = _to_iso(execution.get("finishedAt"))

        end_time = _terminal_end_time(started_at, finished_at, raw_status)
        event_time = finished_at or started_at
        if not event_time:
            # event_time is required; a run with no timestamps can't be reported.
            return None

        event = {
            "job_source_id": pipeline_name,
            "run_source_id": run_id,
            "status": raw_status,
            "event_time": event_time,
            "start_time": started_at,
            "end_time": end_time,
            "trigger": _TRIGGER_MAP.get(execution.get("trigger") or ""),
            "run_url": f"{_RUN_URL_PREFIX}{run_id}" if run_id else None,
        }

        environment_name = execution.get("environmentName")
        if environment_name:
            # The run belongs to one environment (= one group); say which.
            event["group"] = _group(environment_name)

        if raw_status in _ERROR_VENDOR_STATUSES:
            event["error"] = _error_dict(
                execution.get("message"), raw_status, f"Pipeline execution {raw_status}"
            )

        task_runs = self._build_task_runs(
            project_id,
            run_id,
            pipeline_name,
            run_started_at=started_at,
            run_finished_at=finished_at,
        )
        if task_runs:
            event["task_runs"] = task_runs

        return _compact(event)

    def _build_task_runs(
        self,
        project_id: Optional[str],
        run_id: Optional[str],
        job_source_id: str,
        run_started_at: Optional[str] = None,
        run_finished_at: Optional[str] = None,
    ) -> List[dict]:
        """Fetch per-component steps for an execution as nested task-run events.

        Some steps come back with a terminal status but no ``startedAt`` /
        ``finishedAt`` of their own; a task-run still needs a valid
        ``event_time`` (and terminal task-runs need an ``end_time``), so we fall
        back to the parent run's timestamps when a step omits its own.
        """
        if not project_id or not run_id:
            return []
        try:
            body = self._request(
                f"/v1/projects/{project_id}/pipeline-executions/{run_id}/steps",
                {"size": _STEPS_PAGE_SIZE},
            )
        except requests.HTTPError:
            return []
        steps = body.get("results", []) or []

        task_runs: List[dict] = []
        for step in steps:
            task_id = _step_task_id(step)
            if not task_id:
                continue
            result = step.get("result") or {}
            raw_status = result.get("status") or "UNKNOWN"

            started_at = _to_iso(result.get("startedAt")) or run_started_at
            finished_at = _to_iso(result.get("finishedAt")) or run_finished_at
            end_time = _terminal_end_time(started_at, finished_at, raw_status)

            task_run = {
                "job_source_id": job_source_id,
                "run_source_id": step.get("id"),
                "task_source_id": task_id,
                "status": raw_status,
                "event_time": finished_at or started_at,
                "start_time": started_at,
                "end_time": end_time,
            }
            if raw_status in _ERROR_VENDOR_STATUSES:
                task_run["error"] = _error_dict(
                    result.get("message"), raw_status, f"Component {raw_status}"
                )
            task_runs.append(_compact(task_run))
        return task_runs


########################################
# Module-level helpers
########################################


def _group(environment_name: str) -> dict:
    """Build the EtlGroup dict for a Matillion environment.

    The connector is scoped to one project, so an environment name is unique
    within the connection and serves as the group source id.
    """
    return {
        "source_id": environment_name,
        "name": environment_name,
        "group_type": "environment",
    }


def _step_task_id(step: dict) -> Optional[str]:
    """Stable task id for a component step: its name, falling back to its id.

    Used for both the declarative ``tasks`` (metadata) and the runtime
    ``task_runs`` so a task's declaration and its runs share one identity.
    """
    return step.get("name") or step.get("id")


def _pipeline_display_name(pipeline_name: str) -> str:
    """Human-friendly job name: drop Matillion's pipeline file extension.

    ``"failure_pipeline.orch.yaml"`` -> ``"failure_pipeline"``. The full
    ``pipelineName`` stays the ``job_source_id`` (it's the query-tag id).
    """
    for suffix in (".tran.yaml", ".orch.yaml", ".yaml"):
        if pipeline_name.endswith(suffix):
            return pipeline_name[: -len(suffix)]
    return pipeline_name


def _compact(d: dict) -> dict:
    """Drop keys with ``None`` or empty-list values (the agent expects sparse dicts)."""
    return {k: v for k, v in d.items() if v is not None and v != []}


def _terminal_end_time(
    started_at: Optional[str], finished_at: Optional[str], raw_status: str
) -> Optional[str]:
    """End time for a run or task run.

    Uses the reported finish time, falling back to the start time when a
    terminal status omits its own finish (terminal events must carry an
    ``end_time``). Non-terminal events without a finish get ``None``.
    """
    if finished_at:
        return finished_at
    if raw_status in _TERMINAL_VENDOR_STATUSES:
        return started_at
    return None


def _error_dict(message: Optional[str], raw_status: str, default: str) -> dict:
    """Build the ``error`` dict required on failed run/task events."""
    return {"message": message or default, "failure_type": raw_status}


def _to_api_datetime(dt: datetime) -> str:
    """Format a datetime as a UTC ISO-8601 ``...Z`` string for API query params.

    Naive datetimes are assumed UTC (matching :func:`_parse_datetime`) so a
    missing tzinfo can't silently shift the window by the host's UTC offset.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string to a timezone-aware datetime (assume UTC if naive)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_iso(value: Optional[str]) -> Optional[str]:
    """Normalize a vendor timestamp to a timezone-aware ISO-8601 string."""
    dt = _parse_datetime(value)
    return dt.isoformat() if dt else None
