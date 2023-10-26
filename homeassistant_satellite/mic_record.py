import asyncio
import logging
import threading
import time
from typing import AsyncGenerator, Final, List, Optional, Tuple

from .state import State

DEFAULT_ARECORD: Final = "arecord -r 16000 -c 1 -f S16_LE -t raw"
ARECORD_WITH_DEVICE: Final = "arecord -D {device} -r 16000 -c 1 -f S16_LE -t raw"

APP_NAME: Final = "homeassistant_satellite"

RATE: Final = 16000
WIDTH: Final = 2
CHANNELS: Final = 1
SAMPLES_PER_CHUNK = int(0.03 * RATE)  # 30ms

_LOGGER = logging.getLogger()

TimestampChunk = Tuple[int, bytes]
MicStream = AsyncGenerator[TimestampChunk, None]


class UdpServerProtocol(asyncio.Protocol):
    def __init__(
        self,
        state: State,
        queue: "asyncio.Queue[TimestampChunk]",
        bytes_per_chunk: int,
        loop: asyncio.AbstractEventLoop,
    ):
        super().__init__()
        self._state = state
        self._queue = queue
        self._loop = loop
        self._audio_buffer = bytes()
        self._is_first_chunk = False
        self._bytes_per_chunk = bytes_per_chunk

    def datagram_received(self, data, addr):
        if self._state.mic_host is None:
            self._state.mic_host = addr[0]

        if self._is_first_chunk:
            _LOGGER.debug("Receiving audio from client")
            self._is_first_chunk = False

        if self._audio_buffer or (len(data) < self._bytes_per_chunk):
            # Buffer audio if chunks are too small
            self._audio_buffer += data
            if len(self._audio_buffer) < self._bytes_per_chunk:
                return

            data = self._audio_buffer[: self._bytes_per_chunk]
            self._audio_buffer = self._audio_buffer[self._bytes_per_chunk :]

        ts_chunk = (time.monotonic_ns(), data)
        self._loop.call_soon_threadsafe(self._queue.put_nowait(ts_chunk))


async def record_udp(
    port: int,
    state: State,
    host: str = "0.0.0.0",
    samples_per_chunk: int = SAMPLES_PER_CHUNK,
) -> MicStream:
    bytes_per_chunk = samples_per_chunk * WIDTH * CHANNELS

    queue: "asyncio.Queue[TimestampChunk]" = asyncio.Queue()
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(
        lambda: UdpServerProtocol(state, queue, bytes_per_chunk, loop),
        local_addr=(host, port),
        reuse_address=True,
    )

    while state.is_running:
        ts_chunk = await queue.get()
        yield ts_chunk


async def record_subprocess(
    command: List[str],
    state: State,
    samples_per_chunk: int = SAMPLES_PER_CHUNK,
) -> MicStream:
    """Yield mic samples from a subprocess with a timestamp."""
    _LOGGER.debug("Microphone command: %s", command)
    bytes_per_chunk = samples_per_chunk * WIDTH * CHANNELS

    proc = await asyncio.create_subprocess_exec(
        command[0],
        *command[1:],
        stdout=asyncio.subprocess.PIPE,
    )

    assert proc.stdout is not None

    while state.is_running:
        chunk = await proc.stdout.readexactly(bytes_per_chunk)
        if not chunk:
            break

        yield time.monotonic_ns(), chunk


def _pulseaudio_thread_proc(
    server: str,
    device: Optional[str],
    samples_per_chunk: int,
    state: State,
    queue: asyncio.Queue[TimestampChunk],
    loop: asyncio.AbstractEventLoop,
):
    try:
        import pasimple  # only if needed

        server_name = server if server != "__default__" else None

        with pasimple.PaSimple(
            direction=pasimple.PA_STREAM_RECORD,
            server_name=server_name,
            device_name=device,
            app_name=APP_NAME,
            format=pasimple.PA_SAMPLE_S16LE,
            channels=CHANNELS,
            rate=RATE,
            fragsize=samples_per_chunk * WIDTH,
        ) as pa:
            while state.is_running:
                chunk = pa.read(samples_per_chunk * WIDTH)
                ts_chunk = (time.monotonic_ns(), chunk)
                loop.call_soon_threadsafe(queue.put_nowait, ts_chunk)
    except Exception:
        _LOGGER.exception("Unexpected error in pulseaudio recording thread")


async def record_pulseaudio(
    server: str,
    device: Optional[str],
    state: State,
    samples_per_chunk: int = SAMPLES_PER_CHUNK,
) -> MicStream:
    """Yield mic samples with a timestamp."""
    queue: asyncio.Queue[TimestampChunk] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    pulseaudio_thread = threading.Thread(
        target=_pulseaudio_thread_proc,
        args=(server, device, samples_per_chunk, state, queue, loop),
        daemon=True,
    )
    pulseaudio_thread.start()

    while state.is_running:
        ts_chunk = await queue.get()
        yield ts_chunk
