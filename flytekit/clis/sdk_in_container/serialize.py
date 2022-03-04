import logging
import os
import os as _os
import sys
import tarfile as _tarfile
import typing
from enum import Enum as _Enum

import click

from flytekit.clis.sdk_in_container import constants
from flytekit.clis.sdk_in_container.constants import CTX_PACKAGES
from flytekit.configuration import FastSerializationSettings, ImageConfig, SerializationSettings
from flytekit.core import context_manager as flyte_context
from flytekit.exceptions.scopes import system_entry_point
from flytekit.loggers import cli_logger
from flytekit.tools.fast_registration import compute_digest as _compute_digest
from flytekit.tools.fast_registration import filter_tar_file_fn as _filter_tar_file_fn
from flytekit.tools.module_loader import trigger_loading
from flytekit.tools.serialize_helpers import get_registrable_entities, persist_registrable_entities

CTX_IMAGE = "image"
CTX_LOCAL_SRC_ROOT = "local_source_root"
CTX_FLYTEKIT_VIRTUALENV_ROOT = "flytekit_virtualenv_root"
CTX_PYTHON_INTERPRETER = "python_interpreter"


class SerializationMode(_Enum):
    DEFAULT = 0
    FAST = 1


@system_entry_point
def serialize_all(
    pkgs: typing.List[str] = None,
    local_source_root: typing.Optional[str] = None,
    folder: typing.Optional[str] = None,
    mode: typing.Optional[SerializationMode] = None,
    image: typing.Optional[str] = None,
    flytekit_virtualenv_root: typing.Optional[str] = None,
    python_interpreter: typing.Optional[str] = None,
    config_file: typing.Optional[str] = None,
):
    """
    This function will write to the folder specified the following protobuf types ::
        flyteidl.admin.launch_plan_pb2.LaunchPlan
        flyteidl.admin.workflow_pb2.WorkflowSpec
        flyteidl.admin.task_pb2.TaskSpec

    These can be inspected by calling (in the launch plan case) ::
        flyte-cli parse-proto -f filename.pb -p flyteidl.admin.launch_plan_pb2.LaunchPlan

    See :py:class:`flytekit.models.core.identifier.ResourceType` to match the trailing index in the file name with the
    entity type.
    :param pkgs: Dot-delimited Python packages/subpackages to look into for serialization.
    :param local_source_root: Where to start looking for the code.
    :param folder: Where to write the output protobuf files
    :param mode: Regular vs fast
    :param image: The fully qualified and versioned default image to use
    :param flytekit_virtualenv_root: The full path of the virtual env in the container.
    """

    if not (mode == SerializationMode.DEFAULT or mode == SerializationMode.FAST):
        raise AssertionError(f"Unrecognized serialization mode: {mode}")

    serialization_settings = SerializationSettings(
        image_config=ImageConfig.from_config(config_file, img_name=image),
        fast_serialization_settings=FastSerializationSettings(
            enabled=mode == SerializationMode.FAST,
            # TODO: if we want to move the destination dir as a serialization argument, we should initialize it here
        ),
        flytekit_virtualenv_root=flytekit_virtualenv_root,
        python_interpreter=python_interpreter,
        entrypoint_settings=SerializationSettings.default_entrypoint_settings(python_interpreter),
    )

    ctx = flyte_context.FlyteContextManager.current_context().with_serialization_settings(serialization_settings)
    with flyte_context.FlyteContextManager.with_context(ctx) as ctx:
        trigger_loading(pkgs, local_source_root=local_source_root)
        click.echo(f"Found {len(flyte_context.FlyteEntities.entities)} tasks/workflows")
        loaded_entities = get_registrable_entities(ctx)
        if folder is None:
            folder = "."
        persist_registrable_entities(loaded_entities, folder)

        click.secho(f"Successfully serialized {len(loaded_entities)} flyte objects", fg="green")


@click.group("serialize")
@click.option(
    "--image",
    required=False,
    default=lambda: os.environ.get("FLYTE_INTERNAL_IMAGE", ""),
    help="Text tag: e.g. somedocker.com/myimage:someversion123",
)
@click.option(
    "--local-source-root",
    required=False,
    default=lambda: os.getcwd(),
    help="Root dir for python code containing workflow definitions to operate on when not the current working directory"
    "Optional when running `pyflyte serialize` in out of container mode and your code lies outside of your working directory",
)
@click.option(
    "--in-container-config-path",
    required=False,
    help="This is where the configuration for your task lives inside the container. "
    "The reason it needs to be a separate option is because this pyflyte utility cannot know where the Dockerfile "
    "writes the config file to. Required for running `pyflyte serialize` in out of container mode",
)
@click.option(
    "--in-container-virtualenv-root",
    required=False,
    help="DEPRECATED: This flag is ignored! This is the root of the flytekit virtual env in your container. "
    "The reason it needs to be a separate option is because this pyflyte utility cannot know where flytekit is "
    "installed inside your container. Required for running `pyflyte serialize` in out of container mode when "
    "your container installs the flytekit virtualenv outside of the default `/opt/venv`",
)
@click.pass_context
def serialize(ctx, image, local_source_root, in_container_config_path, in_container_virtualenv_root):
    """
    This command produces protobufs for tasks and templates.
    For tasks, one pb file is produced for each task, representing one TaskTemplate object.
    For workflows, one pb file is produced for each workflow, representing a WorkflowClosure object.  The closure
        object contains the WorkflowTemplate, along with the relevant tasks for that workflow.  In lieu of Admin,
        this serialization step will set the URN of the tasks to the fully qualified name of the task function.
    """
    ctx.obj[CTX_IMAGE] = image
    ctx.obj[CTX_LOCAL_SRC_ROOT] = local_source_root
    click.echo("Serializing Flyte elements with image {}".format(image))

    if in_container_virtualenv_root:
        ctx.obj[CTX_FLYTEKIT_VIRTUALENV_ROOT] = in_container_virtualenv_root
        ctx.obj[CTX_PYTHON_INTERPRETER] = os.path.join(in_container_virtualenv_root, "/bin/python3")
    else:
        # For in container serialize we make sure to never accept an override the entrypoint path and determine it here
        # instead.
        import flytekit

        entrypoint_path = _os.path.abspath(flytekit.__file__)
        if entrypoint_path.endswith(".pyc"):
            entrypoint_path = entrypoint_path[:-1]

        ctx.obj[CTX_FLYTEKIT_VIRTUALENV_ROOT] = _os.path.dirname(entrypoint_path)
        ctx.obj[CTX_PYTHON_INTERPRETER] = sys.executable


@click.command("workflows")
# For now let's just assume that the directory needs to exist. If you're docker run -v'ing, docker will create the
# directory for you so it shouldn't be a problem.
@click.option("-f", "--folder", type=click.Path(exists=True))
@click.pass_context
def workflows(ctx, folder=None):
    cli_logger.getLogger().setLevel(logging.DEBUG)

    if folder:
        click.echo(f"Writing output to {folder}")

    pkgs = ctx.obj[CTX_PACKAGES]
    dir = ctx.obj[CTX_LOCAL_SRC_ROOT]
    serialize_all(
        pkgs,
        dir,
        folder,
        SerializationMode.DEFAULT,
        image=ctx.obj[CTX_IMAGE],
        flytekit_virtualenv_root=ctx.obj[CTX_FLYTEKIT_VIRTUALENV_ROOT],
        python_interpreter=ctx.obj[CTX_PYTHON_INTERPRETER],
        config_file=ctx.obj.get(constants.CTX_CONFIG_FILE, None),
    )


@click.group("fast")
@click.pass_context
def fast(ctx):
    pass


@click.command("workflows")
@click.option("-f", "--folder", type=click.Path(exists=True))
@click.pass_context
def fast_workflows(ctx, folder=None):
    cli_logger.getLogger().setLevel(logging.DEBUG)

    if folder:
        click.echo(f"Writing output to {folder}")

    source_dir = ctx.obj[CTX_LOCAL_SRC_ROOT]
    digest = _compute_digest(source_dir)
    folder = folder if folder else ""
    archive_fname = _os.path.join(folder, f"{digest}.tar.gz")
    click.echo(f"Writing compressed archive to {archive_fname}")
    # Write using gzip
    with _tarfile.open(archive_fname, "w:gz") as tar:
        tar.add(source_dir, arcname="", filter=_filter_tar_file_fn)

    pkgs = ctx.obj[CTX_PACKAGES]
    dir = ctx.obj[CTX_LOCAL_SRC_ROOT]
    serialize_all(
        pkgs,
        dir,
        folder,
        SerializationMode.FAST,
        image=ctx.obj[CTX_IMAGE],
        flytekit_virtualenv_root=ctx.obj[CTX_FLYTEKIT_VIRTUALENV_ROOT],
        python_interpreter=ctx.obj[CTX_PYTHON_INTERPRETER],
        config_file=ctx.obj.get(constants.CTX_CONFIG_FILE, None),
    )


fast.add_command(fast_workflows)
serialize.add_command(workflows)
serialize.add_command(fast)
