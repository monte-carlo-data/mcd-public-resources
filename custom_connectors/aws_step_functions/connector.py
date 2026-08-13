"""AWS Step Functions ETL connector.

Maps AWS Step Functions onto Monte Carlo's ETL model:

- **job**  → a Step Functions **state machine**, keyed by its ``stateMachineArn``
- **task** → a **state** in the state machine's definition (Amazon States
  Language). Declared statically in ``fetch_metadata`` from the parsed
  definition, and reported at runtime as ``task_runs`` reconstructed from a
  run's execution history.
- **run**  → an **execution** of a state machine, keyed by its ``executionArn``

There is no ``group`` concept — Step Functions has no notion of the same state
machine hosted in multiple named environments, so the manifest omits it.

Auth is standard AWS: a boto3 ``stepfunctions`` client built from the
credentials in ``connect_args``. ``region_name`` is always required (Step
Functions is regional); how the client authenticates depends on where the
agent runs — default credential chain (instance/task role), assume-role with
auto-refreshing STS credentials (``role_arn`` + optional ``external_id``), or a
static IAM user key (``aws_access_key_id``/``aws_secret_access_key``, plus
``aws_session_token`` for temporary keys).

The identity needs these read-only actions (or the managed
``AWSStepFunctionsReadOnlyAccess`` policy): ``states:ListStateMachines``,
``states:DescribeStateMachine``, ``states:ListExecutions``,
``states:DescribeExecution``, ``states:GetExecutionHistory``.

Notes / limitations:

- Step Functions has no global "list all executions" API — executions are
  listed per state machine — so polling enumerates state machines and lists
  each one's executions, filtering client-side to the collection window.
  ``list_executions`` returns results newest-first by each item's sort time
  (``stopDate`` for finished runs, ``startDate``/``redriveDate`` for running
  ones), so pagination stops once that sort time drops below the window's lower
  bound — see ``_execution_sort_time``.
- ``list_executions`` does not support **EXPRESS** workflows (their history
  lives in CloudWatch Logs); such state machines are skipped during polling.
- State machines carry no schedule of their own (they're triggered externally,
  e.g. by EventBridge), so no ``schedule`` is emitted.
- **Table lineage is not available** from the Step Functions API — states
  invoke services (Lambda, Glue, ECS, …), not warehouse tables — so
  ``inputs``/``outputs`` are intentionally omitted. To get warehouse lineage,
  use **SQL query tagging**: tag each pipeline's SQL with a JSON comment
  carrying ``mcd_job_id`` set to this connector's ``job_source_id`` (the
  ``stateMachineArn``), optionally ``mcd_task_id`` (a state name, i.e. a
  task's ``task_source_id``) and ``mcd_resource_id`` (the connection's
  resource UUID). Monte Carlo ingests these tags via its warehouse query-log
  collection and resolves them back to the jobs/tasks this connector reports.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional

import boto3
from botocore.config import Config
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import ClientError
from botocore.session import get_session

# Retry config: adaptive mode adds a client-side rate limiter plus exponential
# backoff, so collection self-paces on large accounts (where GetExecutionHistory
# per run is the main cost) instead of hammering the API and hitting throttles.
_RETRY_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 5})

# Raw execution statuses that represent a finished run (require ``end_time``).
# PENDING_REDRIVE is intentionally excluded: it only applies to Distributed Map
# child executions (its statusFilter requires a mapRunArn), which this connector
# does not enumerate, and it denotes a run awaiting redrive — not a finished one.
_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"})
# Raw statuses that represent a failure — we always attach an ``error`` dict
# (validators require one for those that normalize to failed/error).
_FAILURE_STATUSES = frozenset({"FAILED", "TIMED_OUT", "ABORTED"})

# History event detail keys that carry an {error, cause} for a failed step —
# used to attribute an error to a leftover (entered-but-never-exited) task run.
# Covers the fail/timeout/start/submit/schedule shapes across task, Lambda, and
# activity states, plus the execution- and map-run-level failures.
_FAIL_DETAIL_KEYS = (
    "taskFailedEventDetails",
    "taskTimedOutEventDetails",
    "taskStartFailedEventDetails",
    "taskSubmitFailedEventDetails",
    "lambdaFunctionFailedEventDetails",
    "lambdaFunctionTimedOutEventDetails",
    "lambdaFunctionStartFailedEventDetails",
    "lambdaFunctionScheduleFailedEventDetails",
    "activityFailedEventDetails",
    "activityTimedOutEventDetails",
    "activityScheduleFailedEventDetails",
    "mapRunFailedEventDetails",
    "executionFailedEventDetails",
    "executionAbortedEventDetails",
    "executionTimedOutEventDetails",
)

# Cap history pagination so a Map state with many iterations can't fan out an
# unbounded number of calls when reconstructing task runs. The trade-off: on a
# larger history the tail is dropped, so states entered before the cut but
# exited after it are unobservable — ``_task_runs_from_history`` therefore
# skips leftover synthesis when the history is truncated (see there), and the
# run-level error is sourced from ``describe_execution`` rather than the
# (possibly truncated) history.
_MAX_HISTORY_EVENTS = 10000
_HISTORY_PAGE_SIZE = 1000


class Connector:
    """ETL connector for AWS Step Functions."""

    credentials: dict

    ########################################
    # Connection Related Methods
    ########################################

    def setup_connection(self) -> None:
        """Build the boto3 Step Functions client and validate credentials.

        Reads from ``self.credentials`` (``connect_args`` in credentials.json):

        - ``region_name`` (required) — AWS region of the Step Functions service
        - ``role_arn`` (optional) — IAM role to assume (auto-refreshing STS creds)
        - ``external_id`` (optional) — external id required by the assumed role
        - ``role_session_name`` (optional) — STS session name for the assumption
        - ``aws_access_key_id`` (optional) — omit to use the default chain
        - ``aws_secret_access_key`` (optional)
        - ``aws_session_token`` (optional) — for temporary base credentials
        - ``endpoint_url`` (optional) — override the service endpoint (testing)

        See the module docstring for how the three credential models compose.
        """
        session = self._build_session()

        self.client = session.client(
            "stepfunctions",
            config=_RETRY_CONFIG,
            endpoint_url=self.credentials.get("endpoint_url") or None,
        )
        self._region = self.client.meta.region_name

        # Fail fast if credentials/region are wrong.
        self.client.list_state_machines(maxResults=1)

    def _base_session_kwargs(self) -> dict:
        """boto3.Session kwargs from explicit credentials (present keys only).

        Omitted keys let boto3 fall back to its default credential chain.
        """
        return {
            key: self.credentials[key]
            for key in (
                "region_name",
                "aws_access_key_id",
                "aws_secret_access_key",
                "aws_session_token",
            )
            if self.credentials.get(key)
        }

    def _build_session(self) -> "boto3.Session":
        """Build the boto3 session, assuming a role when ``role_arn`` is set."""
        role_arn = self.credentials.get("role_arn")
        if not role_arn:
            return boto3.Session(**self._base_session_kwargs())

        external_id = self.credentials.get("external_id")
        session_name = self.credentials.get("role_session_name", "monte-carlo-etl")
        sts = boto3.Session(**self._base_session_kwargs()).client("sts")

        def _refresh() -> dict:
            params = {"RoleArn": role_arn, "RoleSessionName": session_name}
            if external_id:
                params["ExternalId"] = external_id
            creds = sts.assume_role(**params)["Credentials"]
            return {
                "access_key": creds["AccessKeyId"],
                "secret_key": creds["SecretAccessKey"],
                "token": creds["SessionToken"],
                "expiry_time": creds["Expiration"].isoformat(),
            }

        # RefreshableCredentials re-invokes _refresh() before the STS creds
        # expire, so the client keeps working without any token rotation.
        refreshable = RefreshableCredentials.create_from_metadata(
            metadata=_refresh(),
            refresh_using=_refresh,
            method="sts-assume-role",
        )
        botocore_session = get_session()
        botocore_session._credentials = refreshable
        region = self.credentials.get("region_name")
        if region:
            botocore_session.set_config_variable("region", region)
        return boto3.Session(botocore_session=botocore_session)

    def close_connection(self) -> None:
        """No persistent connection to close — boto3 clients are stateless."""
        pass

    ########################################
    # Metadata Fetching
    ########################################

    def fetch_metadata(self, limit: int, offset: int) -> List[dict]:
        """Return a page of state-machine (job) assets.

        State machines are listed, sorted by ARN for a stable page order, then
        sliced by ``(offset, limit)``. Each asset is enriched with its states
        as ``tasks`` (parsed from the ASL definition) and the definition's
        top-level ``Comment`` as the description.
        """
        machines = self._all_state_machines()
        machines.sort(key=lambda m: m["stateMachineArn"])
        return [self._build_asset(m) for m in machines[offset : offset + limit]]

    def _all_state_machines(self) -> List[dict]:
        """List every state machine in the account/region (paginated)."""
        machines: List[dict] = []
        for page in self.client.get_paginator("list_state_machines").paginate():
            machines.extend(page.get("stateMachines", []) or [])
        return machines

    def _build_asset(self, machine: dict) -> dict:
        """Build an EtlAsset dict from a state-machine list item + definition."""
        arn = machine["stateMachineArn"]
        asset = {
            "job_source_id": arn,
            "name": machine.get("name"),
            "job_url": _console_state_machine_url(arn, self._region),
        }
        try:
            definition = self.client.describe_state_machine(stateMachineArn=arn).get(
                "definition"
            )
        except ClientError:
            return _compact(asset)

        if definition:
            asl = json.loads(definition)
            asset["description"] = asl.get("Comment")
            tasks = _tasks_from_definition(asl)
            if tasks:
                asset["tasks"] = tasks
        return _compact(asset)

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
        """List executions started within ``[window_start, window_end)``.

        Step Functions has no cross-machine execution listing, so enumerate
        state machines and list each one's executions. ``list_executions``
        returns results sorted newest-first by each item's *sort time*: finished
        executions by ``stopDate``, running ones by ``startDate``/``redriveDate``
        (see :func:`_execution_sort_time`). Since that sort time is always
        ``>= startDate``, once an item's sort time falls below ``window_start``
        every later item does too — so we stop paging there, which bounds the
        scan to roughly one window's worth of executions rather than all
        history. Membership is tested on ``startDate`` independently, because a
        run can sort recently (by ``stopDate``) yet have started before the
        window. EXPRESS state machines don't support ``list_executions`` and are
        skipped.
        """
        executions: List[dict] = []
        for machine in self._all_state_machines():
            arn = machine["stateMachineArn"]
            try:
                for page in self.client.get_paginator("list_executions").paginate(
                    stateMachineArn=arn
                ):
                    stop_paging = False
                    for execution in page.get("executions", []) or []:
                        sort_time = _execution_sort_time(execution)
                        if sort_time is not None and sort_time < window_start:
                            # List is descending by sort time (>= startDate),
                            # so all remaining items are out of the window too.
                            stop_paging = True
                            break
                        started = _as_aware(execution.get("startDate"))
                        if started is not None and window_start <= started < window_end:
                            executions.append(execution)
                    if stop_paging:
                        break
            except ClientError:
                # EXPRESS workflows (or access-denied machines) — skip.
                continue

        executions.sort(
            key=lambda e: (_iso(e.get("startDate")) or "", e.get("executionArn") or "")
        )
        return executions

    def _fetch_runs_by_id(self, run_ids: List[str]) -> List[dict]:
        """Webhook mode: describe each execution by its ARN."""
        events: List[dict] = []
        for run_id in run_ids:
            try:
                execution = self.client.describe_execution(executionArn=run_id)
            except ClientError:
                continue
            event = self._build_run_event(execution)
            if event is not None:
                events.append(event)
        return events

    def _build_run_event(self, execution: dict) -> Optional[dict]:
        """Convert an execution (list item or describe result) into an EtlRunEvent.

        ``describe_execution`` results carry ``error``/``cause`` directly; list
        items (polling) don't, so for those the error is fetched from
        ``describe_execution`` — authoritative and unaffected by history
        truncation (see :meth:`_run_error`).
        """
        arn = execution.get("executionArn")
        job_source_id = execution.get("stateMachineArn")
        if not arn or not job_source_id:
            return None

        raw_status = execution.get("status") or "RUNNING"
        start_time = _iso(execution.get("startDate"))
        stop_time = _iso(execution.get("stopDate"))
        end_time = stop_time or (
            start_time if raw_status in _TERMINAL_STATUSES else None
        )
        event_time = stop_time or start_time
        if not event_time:
            return None

        event = {
            "job_source_id": job_source_id,
            "run_source_id": arn,
            "status": raw_status,
            "event_time": event_time,
            "start_time": start_time,
            "end_time": end_time,
            "run_url": _console_execution_url(arn, self._region),
        }

        history = self._execution_history(arn)
        truncated = len(history) >= _MAX_HISTORY_EVENTS
        task_runs = _task_runs_from_history(
            history, job_source_id, arn, raw_status, end_time, truncated
        )
        if task_runs:
            event["task_runs"] = task_runs

        if raw_status in _FAILURE_STATUSES:
            event["error"] = self._run_error(execution, raw_status)

        return _compact(event)

    def _run_error(self, execution: dict, raw_status: str) -> dict:
        """Build the ``error`` dict for a failed run.

        Uses the ``error``/``cause`` already on a ``describe_execution`` result
        (webhook mode); for polling list items, which omit them, calls
        ``describe_execution`` — authoritative and truncation-proof, unlike
        scanning the history tail.
        """
        error_name = execution.get("error")
        cause = execution.get("cause")
        if not error_name and not cause:
            try:
                detail = self.client.describe_execution(
                    executionArn=execution["executionArn"]
                )
                error_name = detail.get("error")
                cause = detail.get("cause")
            except ClientError:
                pass
        return _error_dict(error_name, cause, raw_status)

    def _execution_history(self, execution_arn: str) -> List[dict]:
        """Fetch an execution's history events (chronological), best-effort."""
        events: List[dict] = []
        try:
            for page in self.client.get_paginator("get_execution_history").paginate(
                executionArn=execution_arn,
                includeExecutionData=False,
                PaginationConfig={
                    "MaxItems": _MAX_HISTORY_EVENTS,
                    "PageSize": _HISTORY_PAGE_SIZE,
                },
            ):
                events.extend(page.get("events", []) or [])
        except ClientError:
            return []
        return events


########################################
# Module-level helpers
########################################


def _execution_sort_time(execution: dict) -> Optional[datetime]:
    """The time ``list_executions`` sorts this item by (newest-first).

    Finished executions sort by ``stopDate``; running ones by ``redriveDate``
    (if redriven) else ``startDate``. This value is always ``>= startDate``, so
    it's a safe monotonic key for deciding when a page has fallen below the
    collection window.
    """
    return _as_aware(
        execution.get("stopDate")
        or execution.get("redriveDate")
        or execution.get("startDate")
    )


def _tasks_from_definition(asl: dict) -> List[dict]:
    """Build task dicts from an ASL definition, recursing into nested states.

    Each state becomes a task keyed by its bare state name — the same name the
    execution history reports, so static tasks line up with runtime
    ``task_runs``. Parallel ``Branches`` and Map ``ItemProcessor``/``Iterator``
    sub-workflows are expanded so their inner states appear as tasks too.

    ``upstream_task_source_ids`` is inverted from each scope's
    ``Next``/``Default``/choice/catch transitions (edges only reference states
    within the same scope). Each nested sub-workflow's start state additionally
    lists its container state as an upstream, so the graph stays connected
    across the fan-out boundary.
    """
    tasks: List[dict] = []
    _collect_workflow(asl, tasks, parent=None)
    return tasks


def _collect_workflow(workflow: dict, tasks: List[dict], parent: Optional[str]) -> None:
    """Append tasks for one ASL scope (top-level or a nested sub-workflow).

    ``parent`` is the container state name when recursing into a Parallel
    branch or Map processor; it's added as an upstream of the scope's
    ``StartAt`` state to connect the nested graph to its container.
    """
    states = workflow.get("States") or {}
    start_at = workflow.get("StartAt")

    upstream: dict[str, set] = {name: set() for name in states}
    for name, config in states.items():
        for target in _state_transitions(config):
            if target in upstream:
                upstream[target].add(name)

    for name, config in states.items():
        ups = set(upstream[name])
        if parent is not None and name == start_at:
            ups.add(parent)
        task = {
            "task_source_id": name,
            "name": name,
            "task_type": config.get("Type"),
            "upstream_task_source_ids": sorted(ups),
            "triggered_job_source_ids": _triggered_job_source_ids(config),
        }
        tasks.append(_compact(task))
        for sub_workflow in _sub_workflows(config):
            _collect_workflow(sub_workflow, tasks, parent=name)


def _sub_workflows(config: dict) -> List[dict]:
    """Nested sub-workflow objects of a state (Parallel branches, Map processor).

    Each returned object has its own ``StartAt``/``States``. Map uses
    ``ItemProcessor`` (current) or ``Iterator`` (legacy); Parallel uses
    ``Branches``.
    """
    sub_workflows: List[dict] = list(config.get("Branches") or [])
    processor = config.get("ItemProcessor") or config.get("Iterator")
    if processor:
        sub_workflows.append(processor)
    return sub_workflows


def _triggered_job_source_ids(config: dict) -> List[str]:
    """Child job(s) a state starts via the ``startExecution`` integration.

    A state that invokes another state machine carries the child's ARN in its
    task inputs — under ``Arguments.StateMachineArn`` for JSONata state machines
    or ``Parameters.StateMachineArn`` for JSONPath ones. That ARN is the child's
    ``job_source_id``, giving job→job lineage. Matches both the optimized
    (``arn:aws:states:::states:startExecution[.sync[:2]|.waitForTaskToken]``) and
    the AWS SDK (``arn:aws:states:::aws-sdk:sfn:startExecution``) integrations.

    Only a literal ARN is resolvable; a value chosen at runtime — a JSONPath
    ``StateMachineArn.$`` or a JSONata expression like ``{% ... %}`` — can't be
    known at metadata time and is skipped.
    """
    resource = config.get("Resource") or ""
    if not (
        resource.startswith("arn:aws:states:::states:startExecution")
        or resource.startswith("arn:aws:states:::aws-sdk:sfn:startExecution")
    ):
        return []
    # JSONata task inputs live under "Arguments"; JSONPath ones under "Parameters".
    # "Arguments" may also be a JSONata string expression (not a dict), in which
    # case there's no static ARN to read.
    args = config.get("Arguments")
    if not isinstance(args, dict):
        args = config.get("Parameters")
    if not isinstance(args, dict):
        return []
    child_arn = args.get("StateMachineArn")
    if not isinstance(child_arn, str) or not child_arn or child_arn.lstrip().startswith("{%"):
        return []
    return [child_arn]


def _state_transitions(config: dict) -> List[str]:
    """Names of states a given ASL state can transition to."""
    targets: List[str] = []
    if config.get("Next"):
        targets.append(config["Next"])
    if config.get("Default"):
        targets.append(config["Default"])
    for choice in config.get("Choices") or []:
        if choice.get("Next"):
            targets.append(choice["Next"])
    for catch in config.get("Catch") or []:
        if catch.get("Next"):
            targets.append(catch["Next"])
    return targets


def _task_runs_from_history(
    history: List[dict],
    job_source_id: str,
    run_arn: str,
    exec_status: str,
    run_end: Optional[str],
    truncated: bool,
) -> List[dict]:
    """Reconstruct per-state task runs from an execution's history events.

    Every state emits a ``*StateEntered`` event (start) and, on normal
    completion, a ``*StateExited`` event (end). Each run is keyed by its
    ``StateEntered`` event ``id`` — unique and immutable within the execution —
    so a task keeps the same ``run_source_id`` across re-collections of the same
    run (webhook re-describe, overlapping windows, RUNNING → terminal).

    Map iterations and Parallel branches re-enter the *same* state names, so
    starts are tracked as a FIFO queue per name; each exit pairs with the oldest
    still-open start of that name. Task statuses reuse the vendor vocabulary so
    ``run_status_mapping`` covers them without a separate task mapping.

    A state entered but never exited is a *leftover*, resolved from the overall
    execution status (failed → FAILED with the run's error, running → RUNNING,
    otherwise SUCCEEDED). Leftover synthesis is skipped when ``truncated`` is set:
    a dropped history tail makes "not observed exiting" indistinguishable from
    "still open", so synthesizing would fabricate FAILED/durations for states
    that actually completed past the cut.
    """
    open_states: dict[str, List[tuple]] = {}
    runs: List[dict] = []
    last_fail: tuple = (None, None)

    for event in history:
        timestamp = _iso(event.get("timestamp"))
        entered = event.get("stateEnteredEventDetails")
        exited = event.get("stateExitedEventDetails")

        if entered and entered.get("name"):
            open_states.setdefault(entered["name"], []).append(
                (event.get("id"), timestamp)
            )
            continue
        if exited and exited.get("name"):
            name = exited["name"]
            starts = open_states.get(name)
            event_id, start = starts.pop(0) if starts else (event.get("id"), None)
            last_fail = (None, None)  # a clean exit clears any recovered failure
            runs.append(
                _task_run(job_source_id, run_arn, event_id, name, "SUCCEEDED", start, timestamp)
            )
            continue
        for key in _FAIL_DETAIL_KEYS:
            detail = event.get(key)
            if detail:
                last_fail = (detail.get("error"), detail.get("cause"))
                break

    if truncated:
        # History tail dropped — a leftover may simply be an un-fetched exit, so
        # emit only the state pairs actually observed exiting above.
        return runs

    # States entered but never exited — resolve from the execution outcome.
    for name, starts in open_states.items():
        for event_id, start in starts:
            if exec_status in _FAILURE_STATUSES:
                error = _error_dict(last_fail[0], last_fail[1], exec_status)
                runs.append(
                    _task_run(
                        job_source_id, run_arn, event_id, name, "FAILED",
                        start, run_end or start, error=error,
                    )
                )
            elif exec_status == "RUNNING":
                runs.append(
                    _task_run(job_source_id, run_arn, event_id, name, "RUNNING", start, None)
                )
            else:
                runs.append(
                    _task_run(
                        job_source_id, run_arn, event_id, name, "SUCCEEDED", start, run_end or start
                    )
                )
    return runs


def _task_run(
    job_source_id: str,
    run_arn: str,
    event_id,
    name: str,
    status: str,
    start: Optional[str],
    end: Optional[str],
    error: Optional[dict] = None,
) -> dict:
    """Build a nested task-run event dict, keyed by the state's history event id."""
    task_run = {
        "job_source_id": job_source_id,
        "run_source_id": f"{run_arn}#{event_id}",
        "task_source_id": name,
        "name": name,
        "status": status,
        "event_time": end or start,
        "start_time": start,
        "end_time": end,
    }
    if error:
        task_run["error"] = error
    return _compact(task_run)


def _error_dict(
    error_name: Optional[str], cause: Optional[str], raw_status: str
) -> dict:
    """Build an ``error`` dict from a Step Functions error name + cause."""
    return _compact(
        {
            "message": cause or error_name or f"Execution {raw_status}",
            "code": error_name,
            "failure_type": raw_status,
        }
    )


def _console_state_machine_url(arn: str, region: str) -> str:
    """Link to a state machine in the Step Functions console."""
    return (
        f"https://{region}.console.aws.amazon.com/states/home"
        f"?region={region}#/statemachines/view/{arn}"
    )


def _console_execution_url(arn: str, region: str) -> str:
    """Link to an execution in the Step Functions console."""
    return (
        f"https://{region}.console.aws.amazon.com/states/home"
        f"?region={region}#/v2/executions/details/{arn}"
    )


def _compact(d: dict) -> dict:
    """Drop keys with ``None`` or empty-list values (the agent expects sparse dicts)."""
    return {k: v for k, v in d.items() if v is not None and v != []}


def _as_aware(value) -> Optional[datetime]:
    """Coerce a boto3 timestamp (datetime) to timezone-aware UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value) -> Optional[str]:
    """Normalize a boto3 timestamp to a timezone-aware ISO-8601 string."""
    dt = _as_aware(value)
    return dt.isoformat() if dt else None
