import asyncio
import logging
import time
from typing import Any, AsyncGenerator, Dict, Optional, Tuple

from .ha_connection import HAConnection

_LOGGER = logging.getLogger(__name__)


async def stream(
    ha_connection: HAConnection,
    audio: "asyncio.Queue[Tuple[int, bytes]]",
    pipeline_name: Optional[str] = None,
    audio_seconds_to_buffer: float = 0,
) -> AsyncGenerator[Tuple[int, str, Dict[str, Any]], None]:
    """Streams audio to an Assist pipeline and yields events as (timestamp, type, data)."""

    pipeline_id: Optional[str] = None
    if pipeline_name:
        pipeline_id = await _get_pipeline_id(ha_connection, pipeline_name)

    pipeline_events, handler_id = await _start_pipeline(
        ha_connection,
        pipeline_id,
        audio_seconds_to_buffer=audio_seconds_to_buffer,
    )

    async for timestamp, event_type, event_data in _audio_to_events(
        ha_connection,
        pipeline_events,
        handler_id,
        audio,
    ):
        yield timestamp, event_type, event_data


async def _get_pipeline_id(
    ha_connection: HAConnection, pipeline_name: str
) -> Optional[str]:
    """Resolves pipeline id by name."""
    msg = await ha_connection.send_and_receive(
        {
            "type": "assist_pipeline/pipeline/list",
        }
    )
    _LOGGER.debug(msg)

    pipelines = msg["result"]["pipelines"]
    pipeline_id = _find_pipeline_by_name(
        pipeline_name, {p["name"]: p for p in pipelines}
    )

    if not pipeline_id:
        _LOGGER.warning("No pipeline named %s in %s", pipeline_name, pipelines)

    return pipeline_id


async def _start_pipeline(
    ha_connection,
    pipeline_id: Optional[str],
    audio_seconds_to_buffer: float = 0.0,
) -> Tuple[AsyncGenerator[dict, None], int]:
    """Starts Assist pipeline and returns (message id, handler id)"""
    pipeline_args = {
        "type": "assist_pipeline/run",
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

    # send_and_receive_many returns a generator of all responses to our message
    pipeline_events = ha_connection.send_and_receive_many(pipeline_args)

    msg = await pipeline_events.__anext__()
    _LOGGER.debug(msg)
    assert msg["success"], "Pipeline failed to run"

    # Get handler id.
    # This is a single byte prefix that needs to be in every binary payload.
    msg = await pipeline_events.__anext__()
    _LOGGER.debug(msg)
    handler_id = msg["event"]["data"]["runner_data"]["stt_binary_handler_id"]

    return pipeline_events, handler_id


async def _audio_to_events(
    ha_connection: HAConnection,
    pipeline_events: AsyncGenerator[dict, None],
    handler_id: int,
    audio: "asyncio.Queue[Tuple[int, bytes]]",
) -> AsyncGenerator[Tuple[int, str, Dict[str, Any]], None]:
    """Streams audio into pipeline and yields events."""
    prefix_bytes = bytes([handler_id])

    audio_task = asyncio.create_task(audio.get())
    event_task = asyncio.ensure_future(pipeline_events.__anext__())
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
                asyncio.create_task(
                    ha_connection.send_bytes(prefix_bytes + audio_chunk)
                )
            )

            # Next audio chunk
            audio_task = asyncio.create_task(audio.get())
            pending.add(audio_task)

        if event_task in done:
            event = event_task.result()

            assert event["type"] == "event"

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
            event_task = asyncio.ensure_future(pipeline_events.__anext__())
            pending.add(event_task)

    for task in pending:
        task.cancel()


def _find_pipeline_by_name(name: str, pipelines: Dict[str, Any]) -> Optional[str]:
    """Return pipeline id for a name. Try exact match first, following by normalized match."""
    pipeline_info = pipelines.get(name)
    if pipeline_info is not None:
        # Exact match
        return pipeline_info["id"]

    # Normalize and check again
    name_norm = _normalize_pipeline_name(name)
    for pipeline_name, pipeline_info in pipelines.items():
        pipeline_name_norm = _normalize_pipeline_name(pipeline_name)
        if name_norm == pipeline_name_norm:
            return pipeline_info["id"]

    return None


def _normalize_pipeline_name(name: str) -> str:
    return name.strip().casefold()
