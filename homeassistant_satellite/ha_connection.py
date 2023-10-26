import asyncio
import logging
import os
from asyncio import Queue
from typing import Any, AsyncGenerator, Dict, Optional

import aiohttp

_LOGGER = logging.getLogger()

MessageType = Dict[Any, Any]


class HAConnection:
    """
    Class handling all the low level websocket communication with HA.

    Clients should only use the following high-level public functions for communicating with HA:

        send_and_receive(message):       sends JSON message and returns the response

        send_and_receive_many(message):  sends JSON message and returns generator of all responses

        send_bytes(bytes):               sends binary message (without response)

    Responses are properly dispatched to the corresponding message (based on
    their message_id).  So the correct response will always be received, even if
    messages arrive in different order.
    """

    def __init__(self, host: str, protocol: str, token: str, port: int = 8123):
        self._token = token
        self._message_id = 1

        self._message_queues: Dict[
            int, Queue[Optional[MessageType]]
        ] = {}  # msg_id => queue of messages

        ws_protocol = "wss" if protocol == "https" else "ws"
        url = f"{ws_protocol}://{host}:{port}/api/websocket"

        self._session = aiohttp.ClientSession()
        self._websocket_context = self._session.ws_connect(url)

        self.__websocket: Optional[aiohttp.ClientWebSocketResponse] = None
        self.__receive_loop_task: Optional[asyncio.Task] = None

    # Async context manager
    async def __aenter__(self):
        await self._session.__aenter__()
        self.__websocket = await self._websocket_context.__aenter__()

        await self.__authenticate()

        # start the loop after authenticating
        self.__receive_loop_task = asyncio.create_task(self.__receive_loop())

        return self

    async def __aexit__(self, exc_type, exc, tb):
        assert self.__receive_loop_task is not None
        self.__receive_loop_task.cancel()

        await self._websocket_context.__aexit__(exc_type, exc, tb)
        await self._session.__aexit__(exc_type, exc, tb)

    async def __receive_loop(self) -> None:
        """Loop that receives and dispatches messages."""
        assert self.__websocket is not None

        try:
            # Run until the task is cancelled
            async for msg in self.__websocket:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.error("websocket connection error %s", msg)
                    break

                if msg.type != aiohttp.WSMsgType.TEXT:
                    _LOGGER.warning("unknown message type received: %s", msg.type)
                    continue

                # fulfill future, if available, otherwise queue the message
                message = msg.json()
                queue = self._message_queues.get(message["id"])
                if queue is not None:
                    queue.put_nowait(message)
                else:
                    _LOGGER.warning(
                        "no consumer for received message_id %s", message["id"]
                    )

            _LOGGER.error("websocket connection closed")

            # we exit on every error, it's more robust to just restart than to try to recover
            # TODO: more precise error handling
            os._exit(-1)  # pylint: disable=protected-access

        except asyncio.CancelledError:
            _LOGGER.debug("WS receive loop finished")

    async def __authenticate(self) -> None:
        """Authenticate websocket connection to HA"""

        assert self.__websocket is not None
        message = await self.__websocket.receive_json()
        assert message["type"] == "auth_required", message

        # raw send, no message id
        await self.__websocket.send_json(
            {
                "type": "auth",
                "access_token": self._token,
            }
        )

        message = await self.__websocket.receive_json()
        assert message.get("type") == "auth_ok", message
        _LOGGER.info(
            "Authenticated to Home Assistant version %s", message.get("ha_version")
        )

    # -------------------------------------------------------------------------
    # Public functions to communicate with HA
    # -------------------------------------------------------------------------

    async def send_and_receive(self, message: MessageType) -> MessageType:
        """Send JSON message and receives the response"""

        return await self.send_and_receive_many(message).__anext__()

    async def send_and_receive_many(
        self, message: MessageType
    ) -> AsyncGenerator[MessageType, None]:
        """Send JSON message and receives all responses"""

        assert self.__websocket is not None
        assert isinstance(message, dict), "Invalid WS message type"

        try:
            message["id"] = self._message_id
            self._message_id += 1

            queue: Queue[Optional[MessageType]] = Queue()
            self._message_queues[message["id"]] = queue

            _LOGGER.debug("send_json() message=%s", message)

            await self.__websocket.send_json(message)

            while True:
                response = await queue.get()
                _LOGGER.debug("send_and_subscribe_json() response=%s", response)

                if response is None:
                    break

                yield response

        finally:
            # delete the queue when the generator is closed
            del self._message_queues[message["id"]]

    async def send_bytes(self, bts: bytes):
        """Send binary message (without response)"""

        assert self.__websocket is not None
        await self.__websocket.send_bytes(bts)
