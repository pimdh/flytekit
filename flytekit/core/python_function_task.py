"""
=========================================
:mod:`flytekit.core.python_function_task`
=========================================

.. currentmodule:: flytekit.core.python_function_task

.. autosummary::
   :nosignatures:
   :template: custom.rst
   :toctree: generated/

   PythonFunctionTask
   PythonInstanceTask

"""

from __future__ import annotations

import inspect
from abc import ABC
from collections import OrderedDict
from contextlib import suppress
from enum import Enum
from typing import Any, Callable, Iterable, List, Optional, Tuple, TypeVar, Union, cast

from flytekit.configuration import LocalConfig
from flytekit.core import launch_plan as _annotated_launch_plan
from flytekit.core.base_task import Task, TaskResolverMixin
from flytekit.core.context_manager import ExecutionState, FlyteContext, FlyteContextManager
from flytekit.core.docstring import Docstring
from flytekit.core.interface import transform_function_to_interface
from flytekit.core.promise import (
    Promise,
    VoidPromise,
    async_flyte_entity_call_handler,
    translate_inputs_to_literals,
    translate_inputs_to_native,
)
from flytekit.core.python_auto_container import PythonAutoContainerTask, default_task_resolver
from flytekit.core.tracked_abc import FlyteTrackedABC
from flytekit.core.tracker import extract_task_module, is_functools_wrapped_module_level, isnested, istestfunction
from flytekit.core.worker_queue import WorkerQueue
from flytekit.core.workflow import (
    PythonFunctionWorkflow,
    WorkflowBase,
    WorkflowFailurePolicy,
    WorkflowMetadata,
    WorkflowMetadataDefaults,
)
from flytekit.exceptions.user import FlyteValueException
from flytekit.loggers import logger
from flytekit.models import dynamic_job as _dynamic_job
from flytekit.models import literals as _literal_models
from flytekit.models import task as task_models
from flytekit.models.admin import workflow as admin_workflow_models
from flytekit.utils.asyn import loop_manager

T = TypeVar("T")


class PythonInstanceTask(PythonAutoContainerTask[T], ABC):  # type: ignore
    """
    This class should be used as the base class for all Tasks that do not have a user defined function body, but have
    a platform defined execute method. (Execute needs to be overridden). This base class ensures that the module loader
    will invoke the right class automatically, by capturing the module name and variable in the module name.

    .. code-block: python

        x = MyInstanceTask(name="x", .....)

        # this can be invoked as
        x(a=5) # depending on the interface of the defined task

    """

    def __init__(
        self,
        name: str,
        task_config: T,
        task_type: str = "python-task",
        task_resolver: Optional[TaskResolverMixin] = None,
        **kwargs,
    ):
        """
        Please see class level documentation.
        """
        super().__init__(name=name, task_config=task_config, task_type=task_type, task_resolver=task_resolver, **kwargs)


class PythonFunctionTask(PythonAutoContainerTask[T]):  # type: ignore
    """
    A Python Function task should be used as the base for all extensions that have a python function. It will
    automatically detect interface of the python function and when serialized on the hosted Flyte platform handles the
    writing execution command to execute the function

    It is advised this task is used using the @task decorator as follows

    .. code-block: python

        @task
        def my_func(a: int) -> str:
           ...

    In the above code, the name of the function, the module, and the interface (inputs = int and outputs = str) will be
    auto detected.
    """

    class ExecutionBehavior(Enum):
        DEFAULT = 1
        DYNAMIC = 2
        EAGER = 3

    def __init__(
        self,
        task_config: T,
        task_function: Callable,
        task_type="python-task",
        ignore_input_vars: Optional[List[str]] = None,
        execution_mode: ExecutionBehavior = ExecutionBehavior.DEFAULT,
        task_resolver: Optional[TaskResolverMixin] = None,
        node_dependency_hints: Optional[
            Iterable[Union["PythonFunctionTask", "_annotated_launch_plan.LaunchPlan", WorkflowBase]]
        ] = None,
        **kwargs,
    ):
        """
        :param T task_config: Configuration object for Task. Should be a unique type for that specific Task
        :param Callable task_function: Python function that has type annotations and works for the task
        :param Optional[List[str]] ignore_input_vars: When supplied, these input variables will be removed from the
        interface. This
                                  can be used to inject some client side variables only. Prefer using ExecutionParams
        :param Optional[ExecutionBehavior] execution_mode: Defines how the execution should behave, for example
            executing normally or specially handling a dynamic case.
        :param str task_type: String task type to be associated with this Task
        :param Optional[Iterable[Union["PythonFunctionTask", "_annotated_launch_plan.LaunchPlan", WorkflowBase]]]
        node_dependency_hints:
            A list of tasks, launchplans, or workflows that this task depends on. This is only
            for dynamic tasks/workflows, where flyte cannot automatically determine the dependencies prior to runtime.
        """
        if task_function is None:
            raise ValueError("TaskFunction is a required parameter for PythonFunctionTask")
        self._native_interface = transform_function_to_interface(task_function, Docstring(callable_=task_function))
        mutated_interface = self._native_interface.remove_inputs(ignore_input_vars)
        name, _, _, _ = extract_task_module(task_function)
        super().__init__(
            task_type=task_type,
            name=name,
            interface=mutated_interface,
            task_config=task_config,
            task_resolver=task_resolver,
            **kwargs,
        )

        if self._task_resolver is default_task_resolver:
            # The default task resolver can't handle nested functions
            # TODO: Consider moving this to a can_handle function or something inside the resolver itself.
            if (
                not istestfunction(func=task_function)
                and isnested(func=task_function)
                and not is_functools_wrapped_module_level(task_function)
            ):
                raise ValueError(
                    "TaskFunction cannot be a nested/inner or local function. "
                    "It should be accessible at a module level for Flyte to execute it. Test modules with "
                    "names beginning with `test_` are allowed to have nested tasks. "
                    "If you're decorating your task function with custom decorators, use functools.wraps "
                    "or functools.update_wrapper on the function wrapper. "
                    "Alternatively if you want to create your own tasks with custom behavior use the TaskResolverMixin"
                )
        self._task_function = task_function
        self._execution_mode = execution_mode
        self._node_dependency_hints = node_dependency_hints
        if self._node_dependency_hints is not None and self._execution_mode != self.ExecutionBehavior.DYNAMIC:
            raise ValueError(
                "node_dependency_hints should only be used on dynamic tasks. On static tasks and "
                "workflows its redundant because flyte can find the node dependencies automatically"
            )
        self._wf = None  # For dynamic tasks

    @property
    def execution_mode(self) -> ExecutionBehavior:
        return self._execution_mode

    @property
    def node_dependency_hints(
        self,
    ) -> Optional[Iterable[Union["PythonFunctionTask", "_annotated_launch_plan.LaunchPlan", WorkflowBase]]]:
        return self._node_dependency_hints

    @property
    def task_function(self):
        return self._task_function

    @property
    def name(self) -> str:
        """
        Returns the name of the task.
        """
        if self.instantiated_in and self.instantiated_in not in self._name:
            return f"{self.instantiated_in}.{self._name}"
        return self._name

    def execute(self, **kwargs) -> Any:
        """
        This method will be invoked to execute the task. If you do decide to override this method you must also
        handle dynamic tasks or you will no longer be able to use the task as a dynamic task generator.
        """
        if self.execution_mode == self.ExecutionBehavior.DEFAULT:
            # todo:async run task function in a runner if necessary.
            return self._task_function(**kwargs)
        elif self.execution_mode == self.ExecutionBehavior.DYNAMIC:
            return self.dynamic_execute(self._task_function, **kwargs)

    def _create_and_cache_dynamic_workflow(self):
        if self._wf is None:
            workflow_meta = WorkflowMetadata(on_failure=WorkflowFailurePolicy.FAIL_IMMEDIATELY)
            defaults = WorkflowMetadataDefaults(
                interruptible=self.metadata.interruptible if self.metadata.interruptible is not None else False
            )
            self._wf = PythonFunctionWorkflow(self._task_function, metadata=workflow_meta, default_metadata=defaults)

    def compile_into_workflow(
        self, ctx: FlyteContext, task_function: Callable, **kwargs
    ) -> Union[_dynamic_job.DynamicJobSpec, _literal_models.LiteralMap]:
        """
        In the case of dynamic workflows, this function will produce a workflow definition at execution time which will
        then proceed to be executed.
        """
        # TODO: circular import
        from flytekit.core.task import ReferenceTask

        if not ctx.compilation_state:
            cs = ctx.new_compilation_state(prefix="d")
        else:
            cs = ctx.compilation_state.with_params(prefix="d")

        updated_ctx = ctx.with_compilation_state(cs)
        if self.execution_mode == self.ExecutionBehavior.DYNAMIC:
            es = ctx.new_execution_state().with_params(mode=ExecutionState.Mode.DYNAMIC_TASK_EXECUTION)
            updated_ctx = updated_ctx.with_execution_state(es)

        with FlyteContextManager.with_context(updated_ctx):
            # TODO: Resolve circular import
            from flytekit.tools.translator import get_serializable

            self._create_and_cache_dynamic_workflow()
            cast(PythonFunctionWorkflow, self._wf).compile(**kwargs)

            wf = self._wf
            model_entities: OrderedDict = OrderedDict()
            # See comment on reference entity checking a bit down below in this function.
            # This is the only circular dependency between the translator.py module and the rest of the flytekit
            # authoring experience.

            # TODO: After backend support pickling dynamic task, add fast_register_file_uploader to the FlyteContext,
            # and pass the fast_registerfile_uploader to serializer via the options.
            # If during runtime we are execution a dynamic function that is pickled, all subsequent sub-tasks in
            # dynamic should also be pickled. As this is not possible to do during static compilation, we will have to
            # upload the pickled file to the metadata store directly during runtime.
            # If at runtime we are in dynamic task, we will automatically have the fast_register_file_uploader set,
            # so we can use that to pass the file uploader to the translator.
            workflow_spec: admin_workflow_models.WorkflowSpec = get_serializable(
                model_entities, ctx.serialization_settings, wf
            )

            # If no nodes were produced, let's just return the strict outputs
            if len(workflow_spec.template.nodes) == 0:
                return _literal_models.LiteralMap(
                    literals={
                        binding.var: binding.binding.to_literal_model() for binding in workflow_spec.template.outputs
                    }
                )

            # Gather underlying TaskTemplates that get referenced.
            tts = []
            for entity, model in model_entities.items():
                # We only care about gathering tasks here. Launch plans are handled by
                # propeller. Subworkflows should already be in the workflow spec.
                if not isinstance(entity, Task) and not isinstance(entity, task_models.TaskSpec):
                    continue

                # We are currently not supporting reference tasks since these will
                # require a network call to flyteadmin to populate the TaskTemplate
                # model
                if isinstance(entity, ReferenceTask):
                    raise ValueError("Reference tasks are currently unsupported within dynamic tasks")

                if not isinstance(model, task_models.TaskSpec):
                    raise TypeError(
                        f"Unexpected type for serialized form of task. Expected {task_models.TaskSpec}, "
                        f"but got {type(model)}"
                    )

                # Store the valid task template so that we can pass it to the
                # DynamicJobSpec later
                tts.append(model.template)

            dj_spec = _dynamic_job.DynamicJobSpec(
                min_successes=len(workflow_spec.template.nodes),
                tasks=tts,
                nodes=workflow_spec.template.nodes,
                outputs=workflow_spec.template.outputs,
                subworkflows=workflow_spec.sub_workflows,
            )

            return dj_spec

    def dynamic_execute(self, task_function: Callable, **kwargs) -> Any:
        """
        By the time this function is invoked, the local_execute function should have unwrapped the Promises and Flyte
        literal wrappers so that the kwargs we are working with here are now Python native literal values. This
        function is also expected to return Python native literal values.

        Since the user code within a dynamic task constitute a workflow, we have to first compile the workflow, and
        then execute that workflow.

        When running for real in production, the task would stop after the compilation step, and then create a file
        representing that newly generated workflow, instead of executing it.
        """
        ctx = FlyteContextManager.current_context()
        if ctx.execution_state and ctx.execution_state.is_local_execution():
            # The rest of this function mimics the local_execute of the workflow. We can't use the workflow
            # local_execute directly though since that converts inputs into Promises.
            logger.debug(f"Executing Dynamic workflow, using raw inputs {kwargs}")
            self._create_and_cache_dynamic_workflow()
            if self.execution_mode == self.ExecutionBehavior.DYNAMIC:
                es = ctx.new_execution_state().with_params(mode=ExecutionState.Mode.DYNAMIC_TASK_EXECUTION)
            else:
                es = cast(ExecutionState, ctx.execution_state)
            with FlyteContextManager.with_context(ctx.with_execution_state(es)):
                function_outputs = cast(PythonFunctionWorkflow, self._wf).execute(**kwargs)

            if isinstance(function_outputs, VoidPromise) or function_outputs is None:
                return VoidPromise(self.name)

            if len(cast(PythonFunctionWorkflow, self._wf).python_interface.outputs) == 0:
                raise FlyteValueException(function_outputs, "Interface output should've been VoidPromise or None.")

            # TODO: This will need to be cleaned up when we revisit top-level tuple support.
            expected_output_names = list(self.python_interface.outputs.keys())
            if len(expected_output_names) == 1:
                # Here we have to handle the fact that the wf could've been declared with a typing.NamedTuple of
                # length one. That convention is used for naming outputs - and single-length-NamedTuples are
                # particularly troublesome but elegant handling of them is not a high priority
                # Again, we're using the output_tuple_name as a proxy.
                if self.python_interface.output_tuple_name and isinstance(function_outputs, tuple):
                    wf_outputs_as_map = {expected_output_names[0]: function_outputs[0]}
                else:
                    wf_outputs_as_map = {expected_output_names[0]: function_outputs}
            else:
                wf_outputs_as_map = {
                    expected_output_names[i]: function_outputs[i] for i, _ in enumerate(function_outputs)
                }

            # In a normal workflow, we'd repackage the promises coming from tasks into new Promises matching the
            # workflow's interface. For a dynamic workflow, just return the literal map.
            wf_outputs_as_literal_dict = translate_inputs_to_literals(
                ctx,
                wf_outputs_as_map,
                flyte_interface_types=self.interface.outputs,
                native_types=self.python_interface.outputs,
            )
            return _literal_models.LiteralMap(literals=wf_outputs_as_literal_dict)

        if ctx.execution_state and ctx.execution_state.mode == ExecutionState.Mode.TASK_EXECUTION:
            return self.compile_into_workflow(ctx, task_function, **kwargs)

        if ctx.execution_state and ctx.execution_state.mode == ExecutionState.Mode.LOCAL_TASK_EXECUTION:
            return task_function(**kwargs)

        raise ValueError(f"Invalid execution provided, execution state: {ctx.execution_state}")

    def _write_decks(self, native_inputs, native_outputs_as_map, ctx, new_user_params):
        if self._disable_deck is False:
            from flytekit.deck import Deck, DeckField
            from flytekit.deck.renderer import PythonDependencyRenderer

            # These errors are raised if the source code can not be retrieved
            with suppress(OSError, TypeError):
                source_code = inspect.getsource(self._task_function)
                from flytekit.deck.renderer import SourceCodeRenderer

                if DeckField.SOURCE_CODE in self.deck_fields:
                    source_code_deck = Deck(DeckField.SOURCE_CODE.value)
                    renderer = SourceCodeRenderer()
                    source_code_deck.append(renderer.to_html(source_code))

            if DeckField.DEPENDENCIES in self.deck_fields:
                python_dependencies_deck = Deck(DeckField.DEPENDENCIES.value)
                renderer = PythonDependencyRenderer()
                python_dependencies_deck.append(renderer.to_html())

        return super()._write_decks(native_inputs, native_outputs_as_map, ctx, new_user_params)


class AsyncPythonFunctionTask(PythonFunctionTask[T], metaclass=FlyteTrackedABC):
    """
    This is the base task for eager tasks, as well as normal async tasks
    Really only need to override the call function.
    """

    async def __call__(
        self, *args: object, **kwargs: object
    ) -> Union[Tuple[Promise], Promise, VoidPromise, Tuple, None]:
        return await async_flyte_entity_call_handler(self, *args, **kwargs)  # type: ignore

    async def async_execute(self, *args, **kwargs) -> Any:
        """
        Overrides the base execute function. This function does not handle dynamic at all. Eager and dynamic don't mix.
        """
        # Args is present because the asyn helper function passes it, but everything should be in kwargs by this point
        assert not args
        if self.execution_mode == self.ExecutionBehavior.DEFAULT:
            # todo:async run task function in a runner if necessary.
            return await self._task_function(**kwargs)
        elif self.execution_mode == self.ExecutionBehavior.DYNAMIC:
            raise NotImplementedError

    execute = loop_manager.synced(async_execute)


class EagerAsyncPythonFunctionTask(AsyncPythonFunctionTask[T], metaclass=FlyteTrackedABC):
    def __init__(
        self,
        task_config: T,
        task_function: Callable,
        task_type="python-task",
        ignore_input_vars: Optional[List[str]] = None,
        task_resolver: Optional[TaskResolverMixin] = None,
        node_dependency_hints: Optional[
            Iterable[Union["PythonFunctionTask", "_annotated_launch_plan.LaunchPlan", WorkflowBase]]
        ] = None,
        **kwargs,
    ):
        # delete execution mode from kwargs
        if "execution_mode" in kwargs:
            del kwargs["execution_mode"]

        super().__init__(
            task_config,
            task_function,
            task_type,
            ignore_input_vars,
            PythonFunctionTask.ExecutionBehavior.EAGER,
            task_resolver,
            node_dependency_hints,
            **kwargs,
        )

    def local_execution_mode(self) -> ExecutionState.Mode:
        return ExecutionState.Mode.EAGER_LOCAL_EXECUTION

    def local_execute(self, ctx: FlyteContext, **kwargs) -> Union[Tuple[Promise], Promise, VoidPromise, None]:
        """
        update this comment.
        This function is used only in the local execution path and is responsible for calling dispatch execute.
        Use this function when calling a task with native values (or Promises containing Flyte literals derived from
        Python native values).
        """
        native_values = translate_inputs_to_native(
            ctx,
            incoming_values=kwargs,
            flyte_interface_types=self.interface.inputs,
        )

        # if metadata.cache is set, check memoized version
        local_config = LocalConfig.auto()
        if self.metadata.cache and local_config.cache_enabled:
            # todo:async Handle local caching
            raise NotImplementedError
        else:
            output_native_values = self.execute(**native_values)

            return output_native_values

    async def async_execute(self, *args, **kwargs) -> Any:
        """
        Overrides the base execute function. This function does not handle dynamic at all. Eager and dynamic don't mix.
        """
        # Args is present because the asyn helper function passes it, but everything should be in kwargs by this point
        assert not args
        ctx = FlyteContextManager.current_context()
        is_local_execution = cast(ExecutionState, ctx.execution_state).is_local_execution()
        if not is_local_execution:
            # a real execution
            await self.run_with_backend(ctx, **kwargs)
        else:
            # set local mode and proceed with running the function.  This makes the
            mode = self.local_execution_mode()
            with FlyteContextManager.with_context(
                ctx.with_execution_state(cast(ExecutionState, ctx.execution_state).with_params(mode=mode))
            ):
                return await self._task_function(**kwargs)

    execute = loop_manager.synced(async_execute)

    async def run_with_backend(self, ctx: FlyteContext, **kwargs):
        """
        This is the main entry point to kick off a live run. Like if you're running locally, but want to use a
        Flyte backend, or running for real on a Flyte backend.
        """
        # if already a worker queue, then get the execution prefix, and append a new one.
        remote = ctx.flyte_client
        if remote is None:
            raise AssertionError(
                "Remote client needs to be present in the context for cluster-based execution" " of an eager task."
            )

        # set up context
        mode = ExecutionState.Mode.EAGER_EXECUTION
        builder = ctx.with_execution_state(cast(ExecutionState, ctx.execution_state).with_params(mode=mode))

        # ensure that the worker queue is in context
        if not ctx.worker_queue:
            builder = builder.with_worker_queue(WorkerQueue(remote))

        with FlyteContextManager.with_context(builder):
            return await self._task_function(**kwargs)


"""
workflows and launch plans, add assertions, update the call pattern for eager itself, export deck
test remote
signal handling

to enable the async pattern the __call__ function needs to be async or sync. One task type can't be both because it has
to be this function. You can't overload functions in Python, so we have to differentiate at all levels.

eager tasks should run locally - when running a task/workflow, it should be run as if embedded in a workflow context,
with the outputs converted into literals and then back.

first time we're allowing tasks to be called inside other tasks


if i run an eager task, i see it in the console as an execution.
if i run an eager task inside another eager task, do i want to see a separate console link?
yes - inputs to eager tasks should be translated to literals
no - nested eager tasks should handle native inputs.

"""
