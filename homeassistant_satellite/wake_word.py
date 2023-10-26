import asyncio
import logging
import os
from asyncio import Queue
from typing import Optional, Tuple

_LOGGER = logging.getLogger()


class WyomingWakeWordDetector:
    """Detects wake words using a wyoiming protocol server.

    This class is used in the mic thread by syncronous code. However the main
    work is delegated to the async function _run_wyoming which runs in the main thread."""

    def __init__(self, host: str, port: int, wake_word_id: Optional[str]):
        try:
            import wyoming  # noqa: F401
        except ImportError:
            _LOGGER.fatal("Please pip install homeassistant_satellite[wyoming]")
            raise

        self._host = host
        self._port = port
        self._wake_word_id = wake_word_id
        self._detected = False
        self._queue: Optional[Queue] = None
        self._run_wyoming_task: Optional[asyncio.Task] = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._queue:
            # we are connected to wyoming, put None in the queue to trigger a disconnect.
            self._queue.put_nowait(None)

    async def _run_wyoming(self):
        from wyoming.audio import AudioChunk, AudioStart
        from wyoming.client import AsyncTcpClient
        from wyoming.wake import Detect, Detection

        assert self._queue is not None

        try:
            _LOGGER.debug(
                "Connecting to wyoming host=%s port=%s", self._host, self._port
            )

            async with AsyncTcpClient(self._host, self._port) as client:
                _LOGGER.debug("Connected to wyoming")

                await client.write_event(
                    Detect(
                        names=[self._wake_word_id] if self._wake_word_id else None
                    ).event()
                )
                await client.write_event(
                    AudioStart(
                        rate=16000,
                        width=2,
                        channels=1,
                    ).event(),
                )

                # Read audio and wake events in "parallel"
                audio_task = asyncio.create_task(self._queue.get())
                wake_task = asyncio.create_task(client.read_event())
                pending = {audio_task, wake_task}

                while True:
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )

                    if wake_task in done:
                        event = wake_task.result()
                        assert event is not None, "Connection to wyoming lost"

                        _LOGGER.debug("Received wyoming event: %s", event)

                        if Detection.is_type(event.type):
                            # Possible detection
                            detection = Detection.from_event(event)
                            _LOGGER.info(detection)

                            if self._wake_word_id and (
                                detection.name != self._wake_word_id
                            ):
                                _LOGGER.warning(
                                    "Expected wake word %s but got %s, skipping",
                                    self._wake_word_id,
                                    detection.name,
                                )
                                wake_task = asyncio.create_task(client.read_event())
                                pending.add(wake_task)
                                continue

                            self._detected = True
                            break

                        # Next event
                        wake_task = asyncio.create_task(client.read_event())
                        pending.add(wake_task)

                    if audio_task in done:
                        # Forward audio to wake service
                        ts_chunk = audio_task.result()
                        if ts_chunk is None:
                            break  # a None chunk is instruction to exit

                        timestamp, chunk = ts_chunk
                        audio_chunk = AudioChunk(
                            rate=16000,
                            width=2,
                            channels=1,
                            audio=chunk,
                            timestamp=timestamp,
                        )
                        await client.write_event(audio_chunk.event())

                        # Next chunk
                        audio_task = asyncio.create_task(self._queue.get())
                        pending.add(audio_task)

        except Exception:
            _LOGGER.exception("Error running wyoming wake word detection")
            os._exit(-1)  # pylint: disable=protected-access
        finally:
            # Reset
            self._queue = None
            self._run_wyoming_task = None

    @property
    def detected(self) -> bool:
        """Allows to query by syncronous code where a detection was already
        received from the wyoming server."""

        return self._detected

    def reset(self):
        """After a reset, the next call to process_chunk will start a new
        wyoming connection."""

        self._detected = False
        self._queue = None

    async def process_chunk(self, ts_chunk: Tuple[int, bytes]):
        """The first time this function is called a connection to the wyoming
        server is opened (by scheduling _run_wyoming in the main thread). Then
        all chunks are forwarded to _run_wyoming via the queue, to be sent to
        the server.
        """

        if self._queue is None:
            self._queue = Queue()

        if self._run_wyoming_task is None:
            self._run_wyoming_task = asyncio.create_task(self._run_wyoming())

        self._queue.put_nowait(ts_chunk)
