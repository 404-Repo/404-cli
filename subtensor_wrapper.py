#!/usr/bin/env python3
"""
Subtensor wrapper with automatic reconnection on failure.
"""

import asyncio
import logging
import threading
from typing import Any, Optional

import bittensor as bt
from settings import settings


logger = logging.getLogger("uvicorn")


class SubtensorInitError(Exception):
    """Exception raised when subtensor initialization fails."""
    pass


class SubtensorWrapper:
    """
    Wrapper for bittensor async_subtensor with automatic reconnection on failure.
    """

    def __init__(self, endpoint: str, fallback: str | None):
        self._endpoint = endpoint
        self._fallback = fallback
        self._subtensor: bt.async_subtensor | None = None
        self._lock = asyncio.Lock()

    async def _create_connection(self) -> bt.async_subtensor:
        """Create and initialize a new subtensor connection."""
        try:
            logger.debug(f"Attempting to connect to primary endpoint: {self._endpoint}")
            subtensor = bt.async_subtensor(self._endpoint)
            await subtensor.initialize()
            logger.info(f"Successfully connected to primary endpoint: {self._endpoint}")
            return subtensor
        except Exception as e:
            logger.warning(
                f"Failed to connect to primary endpoint {self._endpoint}"
            )
            if self._fallback:
                logger.info(f"Attempting fallback connection to: {self._fallback}")
                try:
                    subtensor = bt.async_subtensor(self._fallback)
                    await subtensor.initialize()
                    logger.info(f"Successfully connected to fallback: {self._fallback}")
                    return subtensor
                except Exception as fallback_error:
                    logger.error(
                        f"Failed to connect to fallback {self._fallback}: {fallback_error}"
                    )
                    raise SubtensorInitError(f"Failed to connect to fallback {self._fallback}: {fallback_error}")
            raise SubtensorInitError(f"Failed to connect to primary endpoint {self._endpoint}: {e}")

    async def ensure_connected(self) -> bt.async_subtensor:
        """Ensure we have a valid connection."""
        async with self._lock:
            if self._subtensor is None:
                self._subtensor = await self._create_connection()
            return self._subtensor

    def __getattr__(self, name: str) -> Any:
        """
        Proxy all attribute access to the underlying subtensor.
        Automatically reconnects on failure.
        """

        async def wrapper(*args, **kwargs):
            try:
                subtensor = await self.ensure_connected()
                method = getattr(subtensor, name)

                result = method(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    return await result
                else:
                    return result
            except BaseException as e:
                logger.debug(f"Method {name} failed, attempting reconnection: {e}")

                async with self._lock:
                    if self._subtensor:
                        try:
                            await self._subtensor.close()
                        except:
                            pass
                        self._subtensor = None

                    self._subtensor = await self._create_connection()

                method = getattr(self._subtensor, name)
                result = method(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    return await result
                else:
                    return result

        return wrapper

    async def close(self):
        """Close the connection."""
        async with self._lock:
            if self._subtensor:
                try:
                    await self._subtensor.close()
                except Exception as e:
                    logger.debug(f"Error closing subtensor: {e}")
                finally:
                    self._subtensor = None


subtensor_wrapper = SubtensorWrapper(endpoint=settings.subtensor_endpoint, fallback=settings.subtensor_fallback)


async def get_subtensor() -> SubtensorWrapper:
    """
    Get the global SubtensorWrapper instance (async version).
    Ensures the connection is established before returning.

    Returns:
        SubtensorWrapper: The connected subtensor wrapper.
    """
    await subtensor_wrapper.ensure_connected()
    return subtensor_wrapper
