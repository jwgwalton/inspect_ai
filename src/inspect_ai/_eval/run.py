import asyncio
import logging
from typing import Any, Awaitable, Callable, Set, cast

from shortuuid import uuid
from typing_extensions import Unpack

from inspect_ai._display import display
from inspect_ai._util.dotenv import dotenv_environ
from inspect_ai._util.error import exception_message
from inspect_ai._util.path import chdir_python
from inspect_ai.log import EvalConfig, EvalLog
from inspect_ai.log._log import Recorder
from inspect_ai.model import GenerateConfig, GenerateConfigArgs
from inspect_ai.solver import Plan, Solver
from inspect_ai.tool._environment.environment import TaskCleanup, TaskInit
from inspect_ai.tool._environment.registry import registry_find_toolenv

from .loader import ResolvedTask
from .task.log import TaskLogger
from .task.run import TaskRunOptions, create_sample_semaphore, task_run
from .task.util import task_run_dir

log = logging.getLogger(__name__)


async def eval_run(
    run_id: str,
    tasks: list[ResolvedTask],
    parallel: int,
    eval_config: EvalConfig,
    recorder: Recorder,
    model_args: dict[str, Any],
    plan: Plan | Solver | list[Solver] | None = None,
    score: bool = True,
    **kwargs: Unpack[GenerateConfigArgs],
) -> list[EvalLog]:
    # we rely on the run_dir and toolenv being the same across all tasks
    # alias these and then confirm that the rest of the tasks conform
    run_dir = task_run_dir(tasks[0].task)
    if any([task_run_dir(task.task) != run_dir for task in tasks]):
        raise RuntimeError("Parallel tasks must have the same working directory.")
    toolenv = next((task.toolenv for task in tasks if task.toolenv is not None), None)

    # if we have a toolenv then we need to enforce sample concurrency at
    # this level of the eval (so we don't explode the # of toolenvs)
    sample_semaphore: asyncio.Semaphore | None = (
        create_sample_semaphore(eval_config, GenerateConfig(**kwargs))
        if toolenv
        else None
    )

    # switch to task directory context
    with chdir_python(run_dir), dotenv_environ():
        # run startup pass for the tool_environment
        shutdown_tool_environments: Callable[[], Awaitable[None]] | None = None
        if toolenv:
            cleanup = eval_config.toolenv_cleanup is not False
            shutdown_tool_environments = await startup_tool_environments(tasks, cleanup)

        try:
            # create run tasks
            task_run_options: list[TaskRunOptions] = []
            for resolved_task in tasks:
                # tasks can provide their own epochs and max_messages
                task = resolved_task.task
                task_eval_config = eval_config.model_copy()
                if task.epochs is not None:
                    task_eval_config.epochs = task.epochs
                if task.max_messages is not None:
                    task_eval_config.max_messages = task.max_messages

                # create and track the logger
                logger = TaskLogger(
                    task_name=task.name,
                    task_version=task.version,
                    task_file=resolved_task.task_file,
                    task_id=resolved_task.id if resolved_task.id else uuid(),
                    run_id=run_id,
                    model=resolved_task.model,
                    dataset=task.dataset,
                    tool_environment=resolved_task.toolenv,
                    task_attribs=task.attribs,
                    task_args=resolved_task.task_args,
                    model_args=model_args,
                    eval_config=task_eval_config,
                    recorder=recorder,
                )

                # append task
                task_run_options.append(
                    TaskRunOptions(
                        task=task,
                        model=resolved_task.model,
                        toolenv=resolved_task.toolenv,
                        logger=logger,
                        config=task_eval_config,
                        plan=plan,
                        score=score,
                        sample_source=resolved_task.sample_source,
                        sample_semaphore=sample_semaphore,
                        kwargs=kwargs,
                    )
                )

            # multiple mode is for running/displaying multiple
            # task definitions, which requires some smart scheduling
            # to ensure that we spread work among models
            if parallel > 1:
                return await run_multiple(task_run_options, parallel)

            # single mode is for a single task definitions (which
            # could in turn be executed for multiple models)
            else:
                return await run_single(task_run_options)

        finally:
            # shutdown tool environments
            if shutdown_tool_environments:
                try:
                    await shutdown_tool_environments()
                except BaseException as ex:
                    log.warning(
                        f"Error occurred shutting down tool environments: {exception_message(ex)}"
                    )


# single mode -- run a single logical task (could consist of multiple
# executable tasks if we are evaluating against multiple models)
async def run_single(tasks: list[TaskRunOptions]) -> list[EvalLog]:
    # https://discuss.python.org/t/asyncio-cancel-a-cancellation-utility-as-a-coroutine-this-time-with-feeling/26304/3
    asyncio_tasks = [asyncio.create_task(task_run(task)) for task in tasks]
    with display().live_task_status(total_tasks=len(tasks), parallel=False):
        try:
            return await asyncio.gather(*asyncio_tasks)
        except asyncio.CancelledError:
            results: list[EvalLog] = []
            for task in asyncio_tasks:
                if task.done():
                    results.append(task.result())
                else:
                    task.cancel()
                    await task
                    results.append(task.result())
        return results


# multiple mode -- run multiple logical tasks (requires some smart
# schedluing to ensure that we are spreading work among models)
async def run_multiple(tasks: list[TaskRunOptions], parallel: int) -> list[EvalLog]:
    # track current usage of each model
    models: Set[str] = set()
    for task in tasks:
        models.add(str(task.model))
    model_counts = {model: 0 for model in models}

    # setup pending tasks, queue, and results
    pending_tasks = tasks.copy()
    queue: asyncio.Queue[TaskRunOptions] = asyncio.Queue()
    results: list[EvalLog] = []
    tasks_completed = 0
    total_tasks = len(tasks)

    async def enque_next_task() -> bool:
        if tasks_completed < total_tasks:
            # find a task that keeps as many different models as possible running concurrently
            model = min(model_counts.items(), key=lambda m: m[1])[0]
            next_task = next((t for t in pending_tasks if str(t.model) == model), None)
            if next_task:
                pending_tasks.remove(next_task)
                model_counts[str(next_task.model)] += 1
                await queue.put(next_task)
                return True
            else:
                return False
        else:
            return False

    async def worker() -> None:
        # worker runs untiil cancelled
        nonlocal tasks_completed
        while True:
            # remove the task from the queue and run it
            task_options = await queue.get()
            task = asyncio.create_task(task_run(task_options))
            try:
                await task
                result = task.result()
                results.append(result)
            except asyncio.CancelledError:
                task.cancel()
                await task
                result = task.result()
                results.append(result)

            # tracking
            tasks_completed += 1
            model_counts[str(task_options.model)] -= 1
            queue.task_done()

            if result.status != "cancelled":
                await enque_next_task()
            else:
                break

    # with task display
    with display().live_task_status(total_tasks=len(tasks), parallel=True):
        # start worker tasks
        workers = [asyncio.create_task(worker()) for _ in range(0, parallel)]

        # enque initial set of tasks
        for _ in range(0, parallel):
            await enque_next_task()

        # wait for all tasks to complete
        try:
            await queue.join()
        except asyncio.CancelledError:
            pass

        # cancel worker tasks
        for w in workers:
            w.cancel()

        return results


async def startup_tool_environments(
    tasks: list[ResolvedTask], cleanup: bool
) -> Callable[[], Awaitable[None]]:
    # find unique toolenvs
    toolenvs: Set[tuple[str, str | None]] = set()
    for task in tasks:
        if task.toolenv is not None and task.toolenv not in toolenvs:
            toolenvs.add(task.toolenv)

    # initialiase toolenvs (track cleanups)
    cleanups: list[tuple[TaskCleanup, str | None]] = []
    for toolenv in toolenvs:
        # find type
        toolenv_type = registry_find_toolenv(toolenv[0])

        # run startup
        task_init = cast(TaskInit, getattr(toolenv_type, "task_init"))
        await task_init("startup", toolenv[1])

        # append cleanup method
        task_cleanup = cast(TaskCleanup, getattr(toolenv_type, "task_cleanup"))
        cleanups.append((task_cleanup, toolenv[1]))

    # return shutdown method
    async def shutdown() -> None:
        for cleanup_jobs in cleanups:
            try:
                cleanup_fn, config = cleanup_jobs
                await cleanup_fn("shutdown", config, cleanup)
            except BaseException as ex:
                log.warning(
                    f"Error occurred shutting down tool environments: {exception_message(ex)}"
                )

    return shutdown
