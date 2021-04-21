import copy
from collections import defaultdict

from contextlib import contextmanager
from types import MappingProxyType
from typing import List, Optional, Dict, Set, Any, Iterator, Iterable

import prefect
from prefect import Flow, Task, Client
from prefect.backend.flow import FlowMetadata
from prefect.backend.task_run import TaskRun
from prefect.engine.state import State
from prefect.utilities.graphql import with_args
from prefect.utilities.logging import get_logger


logger = get_logger("backend.flow_run")


def create_flow_run(
    flow: "Flow" = None,
    flow_id: str = None,
    **kwargs: Any,
) -> "FlowRun":
    """
    Schedule a flow run in the backend given a flow or flow_id. If given a flow, it
    must be registered.

    Args:
        flow: A flow object to lookup a flow id with
        flow_id: A flow id to use for lookup
        **kwargs: Additional kwargs are passed to `client.create_flow_run` and can
            be used to configure the flow run

    Returns:
        An instance of "FlowRun"
    """
    # TODO: The merit of this (and if it belongs in `client.create_flow_run` is still
    #       being assessed

    if flow_id and flow:
        raise ValueError("Either `flow_id` or `flow` may be provided, not both.")

    if flow:
        flow_md = FlowMetadata.from_flow_obj(flow)
        flow_id = flow_md.flow_id

    if not flow_id:
        raise ValueError("`flow_id` or `flow` must be provided.")

    client = Client()
    flow_run_id = client.create_flow_run(flow_id=flow_id, **kwargs)

    return FlowRun.from_flow_run_id(flow_run_id=flow_run_id, load_static_tasks=False)


def execute_flow_run(
    flow_run_id: str,
    flow: "Flow" = None,
    runner_cls: "prefect.engine.flow_runner.FlowRunner" = None,
    **kwargs: Any,
) -> "FlowRun":
    """ "
    The primary entry point for executing a flow run. The flow run will be run
    in-process using the given `runner_cls` which defaults to the `CloudFlowRunner`.

    Args:
        flow_run_id: The flow run id to execute; this run id must exist in the database
        flow: A Flow object can be passed to execute a flow without loading t from
            Storage. If `None`, the flow's Storage metadata will be pulled from the
            API and used to get a functional instance of the Flow and its tasks.
        runner_cls: An optional `FlowRunner` to override the default `CloudFlowRunner`
        **kwargs: Additional kwargs will be passed to the `FlowRunner.run` method

    Returns:
        A `FlowRun` instance with information about the state of the flow run and its
        task runs
    """
    logger.debug(f"Querying for flow run {flow_run_id!r}")

    # Get the `FlowRunner` class type
    # TODO: Respect a config option for this class so it can be overridden by env var,
    #       create a separate config argument for flow runs executed with the backend
    runner_cls = runner_cls or prefect.engine.cloud.flow_runner.CloudFlowRunner

    # Get a `FlowRun` object
    flow_run = FlowRun.from_flow_run_id(
        flow_run_id=flow_run_id, load_static_tasks=False
    )

    logger.info(f"Constructing execution environment for flow run {flow_run_id!r}")

    # Populate global secrets
    secrets = prefect.context.get("secrets", {})
    if flow_run.flow.storage:
        logger.info(f"Loading secrets...")
        for secret in flow_run.flow.storage.secrets:
            with fail_flow_run_on_exception(
                flow_run_id=flow_run_id,
                message=f"Failed to load flow secret {secret!r}: {{exc}}",
            ):
                secrets[secret] = prefect.tasks.secrets.PrefectSecret(name=secret).run()

    # Load the flow from storage if not explicitly provided
    if not flow:
        logger.info(f"Loading flow from {flow_run.flow.storage}...")
        with prefect.context(secrets=secrets, loading_flow=True):
            with fail_flow_run_on_exception(
                flow_run_id=flow_run_id,
                message="Failed to load flow from storage: {exc}",
            ):
                flow = flow_run.flow.storage.get_flow(flow_run.flow.name)

    # Update the run context to include secrets with merging
    run_kwargs = copy.deepcopy(kwargs)
    run_kwargs["context"] = run_kwargs.get("context", {})
    run_kwargs["context"]["secrets"] = {
        # User provided secrets will override secrets we pulled from storage and the
        # current context
        **secrets,
        **run_kwargs["context"].get("secrets", {}),
    }
    # Update some default run kwargs with flow settings
    run_kwargs.setdefault("executor", flow.executor)

    # Execute the flow, this call will block until exit
    logger.info(
        f"Beginning execution of flow run {flow_run.name!r} from {flow_run.flow.name!r} "
        f"with {runner_cls.__name__!r}"
    )
    with prefect.context(flow_run_id=flow_run_id):
        with fail_flow_run_on_exception(
            flow_run_id=flow_run_id,
            message="Failed to execute flow: {exc}",
        ):
            if flow_run.flow.run_config is not None:
                flow_state = runner_cls(flow=flow).run(**run_kwargs)

            # Support for deprecated `flow.environment` use
            else:
                environment = flow.environment
                environment.setup(flow)
                environment.execute(flow)

    logger.info(f"Run finished with final state {flow_state}")
    return flow_run.update()


@contextmanager
def fail_flow_run_on_exception(
    flow_run_id: str,
    message: str = None,
):
    """
    A utility context manager to set the state of the given flow run to 'Failed' if
    an exception occurs. A custom message can be provided for more details and will
    be attached to the state and added to the run logs. KeyboardInterrupts will set
    the flow run state to 'Cancelled' instead and will not use the message. All errors
    will be re-raised.

    Args:
        flow_run_id: The flow run id to update the state of
        message: The message to include in the state and logs. `{exc}` will be formatted
            with the exception details.
    """
    message = message or "Flow run failed with {exc}"
    client = Client()

    try:
        yield
    except KeyboardInterrupt:
        if not FlowRun.from_flow_run_id(flow_run_id).state.is_finished():
            client.set_flow_run_state(
                flow_run_id=flow_run_id,
                state=prefect.engine.state.Cancelled("Keyboard interrupt."),
            )
            logger.warning("Keyboard interrupt. Flow run cancelled, exiting...")
        else:
            logger.warning(
                "Keyboard interrupt. Flow run is already finished, exiting..."
            )
        raise
    except Exception as exc:
        if not FlowRun.from_flow_run_id(flow_run_id).state.is_finished():
            message = message.format(exc=exc.__repr__())
            client.set_flow_run_state(
                flow_run_id=flow_run_id, state=prefect.engine.state.Failed(message)
            )
            client.write_run_logs(
                [
                    dict(
                        flow_run_id=flow_run_id,  # type: ignore
                        name="prefect.backend.api.flow_run.execute_flow_run",
                        message=message,
                        level="ERROR",
                    )
                ]
            )
        logger.error(message, exc_info=True)
        raise


class FlowRun:
    """
    Object for getting information about a Flow Run from the backend API

    Attributes:
        flow_run_id: The uuid of the flow run
        name: The name of the flow run
        flow_id: The uuid of the flow this run is associated with
        state: The state of the flow run
        flow: Metadata for the flow this run is associated with
        task_runs: A view of cached task run metadata associated with this flow run

    """

    def __init__(
        self,
        flow_run_id: str,
        name: str,
        flow_id: str,
        state: State,
        task_runs: Iterable["TaskRun"] = None,
    ):
        self.flow_run_id = flow_run_id
        self.name = name
        self.flow_id = flow_id
        self.state = state

        # Cached value of all task run ids for this flow run, only cached if the flow
        # is done running
        self._task_run_ids: Optional[List[str]] = None

        # Cached value of flow metadata
        self._flow: Optional[FlowMetadata] = None

        # Store a mapping of task run ids to task runs
        self._cached_task_runs: Dict[str, "TaskRun"] = {}

        # Store a mapping of slugs to task run ids (mapped tasks share a slug)
        self._task_slug_to_task_run_ids: Dict[str, Set[str]] = defaultdict(set)

        if task_runs is not None:
            for task_run in task_runs:
                self._cache_task_run_if_finished(task_run)

    def _cache_task_run_if_finished(self, task_run: "TaskRun") -> None:
        """
        Add a task run to the cache if it is in a finished state

        Args:
            task_run: The task run to add
        """
        if task_run.state.is_finished():
            self._cached_task_runs[task_run.task_run_id] = task_run
            self._task_slug_to_task_run_ids[task_run.task_slug].add(
                task_run.task_run_id
            )

    def update(self, load_static_tasks: bool = False) -> "FlowRun":
        """
        Get the a new copy of this object with the newest data from the API

        Args:
            load_static_tasks: Pre-populate the task runs with results from flow tasks
                that are unmapped. Defaults to `False` because it may be wasteful to
                query for task run data when cached tasks are already copied over
                from the old object.

        Returns:
            A new instance of FlowRun
        """
        return self.from_flow_run_id(
            flow_run_id=self.flow_run_id,
            load_static_tasks=load_static_tasks,
            _cached_task_runs=self._cached_task_runs.values(),
        )

    @property
    def flow(self) -> "FlowMetadata":
        """
        Flow metadata for the flow associated with this flow run. Lazily loaded at call
        time then cached for future calls.

        Returns:
            An instance of FlowMetadata
        """
        if self._flow is None:
            self._flow = FlowMetadata.from_flow_id(flow_id=self.flow_id)

        return self._flow

    @property
    def cached_task_runs(self) -> MappingProxyType:
        """
        A view of all cached task runs from this flow run. To pull new task runs, see
        `get`.

        Returns:
            A proxy view of the cached task run dict mapping
                task_run_id -> task run data
        """
        return MappingProxyType(self._cached_task_runs)

    @classmethod
    def from_flow_run_id(
        cls,
        flow_run_id: str,
        load_static_tasks: bool = True,
        _cached_task_runs: Iterable["TaskRun"] = None,
    ) -> "FlowRun":
        """
        Get an instance of this class filled with information by querying for the given
        flow run id

        Args:
            flow_run_id: the flow run id to lookup
            load_static_tasks: Pre-populate the task runs with results from flow tasks
                that are unmapped.
            _cached_task_runs: Pre-populate the task runs with an existing iterable of
               task runs

        Returns:
            A populated `FlowRun` instance
        """
        client = Client()

        flow_run_query = {
            "query": {
                with_args("flow_run_by_pk", {"id": flow_run_id}): {
                    "id": True,
                    "name": True,
                    "flow_id": True,
                    "serialized_state": True,
                    "flow": {"storage", "run_config", "name"},
                }
            }
        }

        result = client.graphql(flow_run_query)
        flow_run = result.get("data", {}).get("flow_run_by_pk", None)

        if not flow_run:
            raise ValueError(
                f"Received bad result while querying for flow run {flow_run_id}: "
                f"{result}"
            )

        if load_static_tasks:
            task_run_data = TaskRun.query_for_task_runs(
                where={
                    "map_index": {"_eq": -1},
                    "flow_run_id": {"_eq": flow_run_id},
                },
                many=True,
            )
            task_runs = [TaskRun.from_task_run_data(data) for data in task_run_data]

        else:
            task_runs = []

        # Combine with the provided `_cached_task_runs` iterable
        task_runs = task_runs + list(_cached_task_runs or [])

        flow_run_id = flow_run.pop("id")

        return cls(
            flow_run_id=flow_run_id,
            flow_id=flow_run.flow_id,
            task_runs=task_runs,
            state=State.deserialize(flow_run.serialized_state),
            name=flow_run.name,
        )

    def get_task_run(
        self, task: Task = None, task_slug: str = None, task_run_id: str = None
    ) -> "TaskRun":
        """
        Get information about a task run from this flow run. Lookup is available by one
        of the following arguments. If the task information is not available locally
        already, we will query the database for it. If multiple arguments are provided,
        we will validate that they are consistent with each other.

        All retrieved task runs that are finished will be cached to avoid re-querying in
        repeated calls

        Args:
            task: A `prefect.Task` object to use for the lookup. The slug will be
                pulled from the task to actually perform the query
            task_slug: A task slug string to use for the lookup
            task_run_id: A task run uuid to use for the lookup

        Returns:
            A cached or newly constructed TaskRun instance
        """

        if task is not None:
            if not task.slug and not task_slug:
                raise ValueError(
                    f"Given task {task} does not have a `slug` set and cannot be "
                    "used for lookups; this generally occurs when the flow has not "
                    "been registered or serialized."
                )

            if task_slug is not None and task_slug != task.slug:
                raise ValueError(
                    "Both `task` and `task_slug` were provided but they contain "
                    "different slug values! "
                    f"`task.slug == {task.slug!r}` and `task_slug == {task_slug!r}`"
                )

            # If they've passed a task_slug that matches this task's slug we can
            # basically ignore the passed `task` object
            if not task_slug:
                task_slug = task.slug

        if task_run_id is not None:
            # Load from the cache if available or query for results
            result = (
                self.task_runs[task_run_id]
                if task_run_id in self._cached_task_runs
                else TaskRun.from_task_run_id(task_run_id)
            )

            if task_slug is not None and result.task_slug != task_slug:
                raise ValueError(
                    "Both `task_slug` and `task_run_id` were provided but the task "
                    "found using `task_run_id` has a different slug! "
                    f"`task_slug == {task_slug!r}` and "
                    f"`result.task_slug == {result.task_slug!r}`"
                )

            self._cache_task_run_if_finished(result)
            return result

        if task_slug is not None:

            if task_slug in self._task_slug_to_task_run_ids:
                task_run_ids = self._task_slug_to_task_run_ids[task_slug]

                # Check for the 'base' task, for unmapped tasks there should always
                # just be one run id but for mapped tasks there will be multiple
                for task_run_id in task_run_ids:
                    result = self.task_runs[task_run_id]
                    if result.map_index == -1:
                        return result

                # We did not find the base mapped task in the cache so we'll
                # drop through to query for it

            result = TaskRun.from_task_slug(
                task_slug=task_slug, flow_run_id=self.flow_run_id
            )
            self._cache_task_run_if_finished(result)
            return result

        raise ValueError(
            "One of `task_run_id`, `task`, or `task_slug` must be provided!"
        )

    def iter_mapped_task_runs(
        self,
        task: Task = None,
        task_slug: str = None,
        cache_results: bool = True,
    ) -> Iterator["TaskRun"]:
        """
        Iterate over the results of a mapped task, yielding a `TaskRun` for each map
        index. This query is not performed in bulk so the results can be lazily
        consumed. If you want all of the task results at once, use `get_task_run` on
        the mapped task instead.

        Args:
            task: A `prefect.Task` object to use for the lookup. The slug will be
                pulled from the task to actually perform the query
            task_slug: A task slug string to use for the lookup
            cache_results: By default, task run data is cached for future lookups.
                However, since we lazily generate the mapped results, caching can be
                disabled to reduce memory consumption for large mapped tasks.

        Yields:
            A TaskRun object for each mapped item
        """
        if task is not None:
            if task_slug is not None and task_slug != task.slug:
                raise ValueError(
                    "Both `task` and `task_slug` were provided but "
                    f"`task.slug == {task.slug!r}` and `task_slug == {task_slug!r}`"
                )
            task_slug = task.slug

        if task_slug is None:
            raise ValueError("Either `task` or `task_slug` must be provided!")

        where = lambda index: {
            "task": {"slug": {"_eq": task_slug}},
            "flow_run_id": {"_eq": self.flow_run_id},
            "map_index": {"_eq": index},
        }
        task_run = TaskRun.from_task_run_data(
            TaskRun.query_for_task_runs(where=where(-1))
        )
        if not task_run.state.is_mapped():
            raise TypeError(
                f"Task run {task_run.task_run_id!r} ({task_run.slug}) is not a mapped "
                f"task."
            )

        map_index = 0
        while task_run:
            task_run_data = TaskRun.query_for_task_runs(
                where=where(map_index), error_on_empty=False
            )
            if not task_run_data:
                break

            task_run = TaskRun.from_task_run_data(task_run_data)

            # Allow the user to skip the cache if they have a lot of task runs
            if cache_results:
                self._cache_task_run_if_finished(task_run)

            yield task_run

            map_index += 1

    def get_all_task_runs(self) -> List["TaskRun"]:
        """
        Get all task runs for this flow run in a single query. Finished task run data
        is cached so future lookups do not query the backend.

        Returns:
            A list of TaskRun objects
        """
        if len(self.task_run_ids) > 1000:
            raise ValueError(
                "Refusing to `get_all_task_runs` for a flow with more than 1000 tasks. "
                "Please load the tasks you are interested in individually."
            )

        # Run a single query instead of querying for each task run separately
        task_run_data = TaskRun.query_for_task_runs(
            where={
                "flow_run_id": {"_eq": self.flow_run_id},
                "task_run_id": {"_not", {"_in": list(self._cached_task_runs.keys())}},
            },
            many=True,
        )
        task_runs = [TaskRun.from_task_run_data(data) for data in task_run_data]
        # Add to cache
        for task_run in task_runs:
            self._cache_task_run_if_finished(task_run)

        return task_runs + list(self._cached_task_runs.values())

    @property
    def task_run_ids(self) -> List[str]:
        """
        Get all task run ids associated with this flow run. Lazily loaded at call time
        then cached for future calls.

        Returns:
            A list of string task run ids
        """
        # Return the cached value immediately if it exists
        if self._task_run_ids:
            return self._task_run_ids

        client = Client()

        task_query = {
            "query": {
                with_args(
                    "task_run",
                    {
                        "where": {
                            "flow_run_id": {"_eq": self.flow_run_id},
                        }
                    },
                ): {
                    "id": True,
                }
            }
        }
        result = client.graphql(task_query)
        task_runs = result.get("data", {}).get("task_run", None)

        if task_runs is None:
            logger.warning(
                f"Failed to load task run ids for flow run {self.flow_run_id}: "
                f"{result}"
            )

        task_run_ids = [task_run["id"] for task_run in task_runs]

        # If the flow run is done, we can safely cache this value
        if self.state.is_finished():
            self._task_run_ids = task_run_ids

        return task_run_ids

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}"
            f"("
            + ", ".join(
                [
                    f"flow_run_id={self.flow_run_id!r}",
                    f"name={self.name!r}",
                    f"state={self.state!r}",
                    f"cached_task_runs={len(self.task_runs)}",
                ]
            )
            + f")"
        )