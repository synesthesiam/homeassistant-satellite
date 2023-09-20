import asyncio
import json
import logging
import time
from typing import Any, AsyncGenerator, Dict, Optional, Tuple

import aiohttp

_LOGGER = logging.getLogger(__name__)


async def stream(
    host: str,
    token: str,
    audio: "asyncio.Queue[Tuple[int, bytes]]",
    pipeline_name: Optional[str] = None,
    port: int = 8123,
    api_path: str = "/api",
    audio_seconds_to_buffer: float = 0,
) -> AsyncGenerator[Tuple[int, str, Dict[str, Any]], None]:
    """Streams audio to an Assist pipeline and yields events as (timestamp, type, data)."""
    url = f"ws://{host}:{port}{api_path}/websocket"
    message_id = 1

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url) as websocket:
            await _authenticate(websocket, token)

            pipeline_id: Optional[str] = None
            if pipeline_name:
                message_id, pipeline_id = await _get_pipeline_id(
                    websocket, message_id, pipeline_name
                )

            message_id, handler_id = await _start_pipeline(
                websocket,
                message_id,
                pipeline_id,
                audio_seconds_to_buffer=audio_seconds_to_buffer,
            )

            async for timestamp, event_type, event_data in _audio_to_events(
                websocket,
                handler_id,
                audio,
            ):
                yield timestamp, event_type, event_data


async def _authenticate(websocket, token: str):
    """Authenticates with HA using a long-lived access token."""
    msg = await websocket.receive_json()
    _LOGGER.debug(msg)
    assert msg["type"] == "auth_required", msg
    await websocket.send_json(
        {
            "type": "auth",
            "access_token": token,
        }
    )

    msg = await websocket.receive_json()
    _LOGGER.debug(msg)
    assert msg["type"] == "auth_ok", msg


async def _get_pipeline_id(
    websocket, message_id: int, pipeline_name: str
) -> Tuple[int, Optional[str]]:
    """Resolves pipeline id by name."""
    pipeline_id: Optional[str] = None
    await websocket.send_json(
        {
            "type": "assist_pipeline/pipeline/list",
            "id": message_id,
        }
    )
    msg = await websocket.receive_json()
    _LOGGER.debug(msg)
    message_id += 1

    pipelines = msg["result"]["pipelines"]
    for pipeline in pipelines:
        if pipeline["name"] == pipeline_name:
            pipeline_id = pipeline["id"]
            break

    if not pipeline_id:
        _LOGGER.warning("No pipeline named %s in %s", pipeline_name, pipelines)

    return message_id, pipeline_id


async def _start_pipeline(
    websocket,
    message_id: int,
    pipeline_id: Optional[str],
    audio_seconds_to_buffer: float = 0.0,
) -> Tuple[int, int]:
    """Starts Assist pipeline and returns (message id, handler id)"""
    pipeline_args = {
        "type": "assist_pipeline/run",
        "id": message_id,
        "start_stage": "wake_word",
        "end_stage": "tts",
        "input": {
            "sample_rate": 16000,
            "timeout": 3,
            "audio_seconds_to_buffer": audio_seconds_to_buffer,
        },
    }
    if pipeline_id:
        pipeline_args["pipeline"] = pipeline_id
    await websocket.send_json(pipeline_args)
    message_id += 1

    msg = await websocket.receive_json()
    _LOGGER.debug(msg)
    assert msg["success"], "Pipeline failed to run"

    # Get handler id.
    # This is a single byte prefix that needs to be in every binary payload.
    msg = await websocket.receive_json()
    _LOGGER.debug(msg)
    handler_id = msg["event"]["data"]["runner_data"]["stt_binary_handler_id"]

    return message_id, handler_id


async def _audio_to_events(
    websocket,
    handler_id: int,
    audio: "asyncio.Queue[Tuple[int, bytes]]",
) -> AsyncGenerator[Tuple[int, str, Dict[str, Any]], None]:
    """Streams audio into pipeline and yields events."""
    prefix_bytes = bytes([handler_id])

    audio_task = asyncio.create_task(audio.get())
    event_task = asyncio.create_task(websocket.receive())
    pending = {audio_task, event_task}

    while True:
        done, pending = await asyncio.wait(
            pending,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if audio_task in done:
            # Forward to websocket
            _timestamp, audio_chunk = audio_task.result()
            pending.add(
                asyncio.create_task(websocket.send_bytes(prefix_bytes + audio_chunk))
            )

            # Next audio chunk
            audio_task = asyncio.create_task(audio.get())
            pending.add(audio_task)

        if event_task in done:
            msg = event_task.result()
            if msg.type != aiohttp.WSMsgType.TEXT:
                _LOGGER.warning("Unexpected message: %s", msg)
                continue

            event = json.loads(msg.data)

            if event.get("type") != "event":
                continue

            _LOGGER.debug(event)
            event_type = event["event"]["type"]
            event_data = event["event"]["data"]
            yield time.monotonic_ns(), event_type, event_data

            if event_type == "run-end":
                _LOGGER.debug("Pipeline finished")
                break

            if (event_type == "error") and (
                event_data.get("code") != "wake-word-timeout"
            ):
                _LOGGER.error(event_data["message"])
                break

            # Next event
            event_task = asyncio.create_task(websocket.receive())
            pending.add(event_task)

    for task in pending:
        task.cancel()
