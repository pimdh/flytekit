import os

import py
import pytest

from flytekit.common.tasks.sdk_runnable import SecretsManager
from flytekit.configuration import secrets


def test_secrets_manager_default():
    with pytest.raises(ValueError):
        sec = SecretsManager()
        sec.get("test")


def test_secrets_manager_get_envvar():
    sec = SecretsManager()
    assert sec.get_secrets_env_var("test") == f"{secrets.SECRETS_ENV_PREFIX.get()}TEST"
    assert sec.get_secrets_env_var("test", secrets_group="group") == f"{secrets.SECRETS_ENV_PREFIX.get()}GROUP.TEST"


def test_secrets_manager_get_file():
    sec = SecretsManager()
    assert sec.get_secrets_file("test") == os.path.join(
        secrets.SECRETS_DEFAULT_DIR.get(), f"{secrets.SECRETS_FILE_PREFIX.get()}test",
    )
    assert sec.get_secrets_file("test", secrets_group="group") == os.path.join(
        secrets.SECRETS_DEFAULT_DIR.get(), "group", f"{secrets.SECRETS_FILE_PREFIX.get()}test",
    )


def test_secrets_manager_file(tmpdir: py.path.local):
    tmp = tmpdir.mkdir("file_test").dirname
    os.environ["FLYTE_SECRETS_DEFAULT_DIR"] = tmp
    sec = SecretsManager()

    f = os.path.join(tmp, "test")
    with open(f, "w+") as w:
        w.write("my-password")
    assert sec.get_secrets_file("test") == f
    assert sec.get("test") == "my-password"

    # Group dir not exists
    with pytest.raises(ValueError):
        sec.get("test", secrets_group="group")

    g = os.path.join(tmp, "group")
    os.makedirs(g)
    f = os.path.join(g, "test")
    with open(f, "w+") as w:
        w.write("my-password")
    assert sec.get("test", secrets_group="group") == "my-password"
    del os.environ["FLYTE_SECRETS_DEFAULT_DIR"]


def test_secrets_manager_bad_env():
    with pytest.raises(ValueError):
        os.environ["TEST"] = "value"
        sec = SecretsManager()
        sec.get("test")

    # Looking for group, but no group present
    with pytest.raises(ValueError):
        os.environ[sec.get_secrets_env_var("test")] = "value"
        sec = SecretsManager()
        sec.get("test", secrets_group="group")


def test_secrets_manager_env():
    sec = SecretsManager()
    os.environ[sec.get_secrets_env_var("test")] = "value"
    assert sec.get("test") == "value"

    os.environ[sec.get_secrets_env_var("test", secrets_group="group")] = "value"
    assert sec.get("test", secrets_group="group") == "value"
