# -*- coding: utf-8 -*-
"""
Purpose: Provides a base class for HTTP clients that limits the number
of connections to a single host to one, so that we don't overwhelm the
server.

This is a workaround for the fact that Oobabooga, at the time of writing,
could not support multiple pending requests at the same time without failing.
"""

import abc
import asyncio
import re
import socket

import aiohttp


class OobaHttpClientError(Exception):
    """
    Purpose: Exception class for OobaHttpClient
    """


class SerializedHttpClient(abc.ABC):
    """
    Purpose: Limits the number of connections to a single host
    to one, so that we don't overwhelm the server.
    """

    HTTP_CLIENT_TIMEOUT_SECONDS: aiohttp.ClientTimeout = aiohttp.ClientTimeout(
        total=None,
        connect=None,
        sock_connect=5.0,
        sock_read=None,
    )

    URL_EXTRACTOR = re.compile(
            r"(https?://(?:(?:[a-zA-Z0-9\-]+\.)*[a-zA-Z0-9\-]+(?:\:\d{1,5})?))(\S+)?"
        )

    @abc.abstractmethod
    async def _setup(self):
        # it's ok to raise an exception here, it will be caught
        ...

    async def setup(self):
        """
        Attempt to connect to the server.

        Returns:
            nothing, if the connection test was successful

        Raises:
            OobaHttpClientError, if the connection fails
        """
        try:
            await self._setup()
            self.is_set_up = True
        except (
            OobaHttpClientError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientError,
            ConnectionRefusedError,
            socket.gaierror,
            asyncio.exceptions.TimeoutError,
        ) as err:
            raise OobaHttpClientError(
                f"Could not connect to {self.service_name} server: {err}"
            ) from err

    def __init__(self, service_name: str, base_url: str):
        self.service_name = service_name
        self.base_url = base_url
        self._session = None
        self.is_set_up = False

    def _get_session(self) -> aiohttp.ClientSession:
        """
        Returns: the session, if it exists
        Raises: OobaHttpClientError if the session does not exist.
        """
        if not self._session:
            raise OobaHttpClientError("Session not initialized")
        return self._session

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(limit_per_host=1)
        try:
            self._session = aiohttp.ClientSession(
                base_url=self.base_url,
                connector=connector,
                timeout=self.HTTP_CLIENT_TIMEOUT_SECONDS,
            )
        except AssertionError as err:
            raise OobaHttpClientError(
                f"Could not connect to {self.service_name} server: {self.base_url}\n"
                + "Ensure the base URL does not have a path component."
            ) from err
        return self

    async def __aexit__(self, *_err):
        if self._session:
            await self._session.close()
        self._session = None

    async def _try_setup(self):
        async with self:
            await self.setup()

    def test_connection(self) -> None:
        try:
            asyncio.run(self._try_setup())
        except AssertionError as err:
            # asyncio will throw an AssertionError if we try to run
            # with a base_url that has a path. This is a user-supplied
            # value, so catching this is grody but necessary.
            raise OobaHttpClientError(
                f"Could not connect to {self.service_name} server: {self.base_url}\n"
                + "Ensure the base URL does not have a path component."
            ) from err
