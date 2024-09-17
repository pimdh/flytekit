
import asyncio
import functools

from typing_extensions import Callable, Any
from types import CoroutineType
from flytekit.loggers import logger


AsyncFuncType = Callable[[Any], CoroutineType]
Synced = Callable[[Any], Any]


def ensure_no_loop(error_msg: str):
    try:
        asyncio.get_running_loop()
        raise AssertionError(error_msg)
    except RuntimeError as e:
        if "no running event loop" not in str(e):
            logger.error(f"Unknown RuntimeError {str(e)}")
            raise


def ensure_and_get_running_loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_running_loop()
    except RuntimeError as e:
        if "no running event loop" not in str(e):
            logger.error(f"Unknown RuntimeError {str(e)}")
            raise


def top_level_sync(func: AsyncFuncType, *args, **kwargs):
    """
    Make sure there is no current loop. Then run the func in a new loop
    """
    ensure_no_loop(f"Calling {func.__name__} when event loop active not allowed")
    coro = func(*args, **kwargs)
    return asyncio.run(coro)


def top_level_sync_wrapper(func: AsyncFuncType) -> Synced:
    """Given a function, make so can be called in async or blocking contexts

    Leave obj=None if defining within a class. Pass the instance if attaching
    as an attribute of the instance.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return top_level_sync(func, *args, **kwargs)

    return wrapper
