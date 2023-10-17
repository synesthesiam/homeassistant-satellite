import logging
import subprocess
import time
from typing import Final, Iterable, List, Optional, Tuple

from .state import State

DEFAULT_ARECORD: Final = "arecord -r 16000 -c 1 -f S16_LE -t raw"
ARECORD_WITH_DEVICE: Final = "arecord -D {device} -r 16000 -c 1 -f S16_LE -t raw"

APP_NAME: Final = "homeassistant_satellite"

RATE: Final = 16000
WIDTH: Final = 2
CHANNELS: Final = 1
SAMPLES_PER_CHUNK = int(0.03 * RATE)  # 30ms

_LOGGER = logging.getLogger()

MicStream = Iterable[Tuple[int, bytes]]


def record_udp(
    port: int,
    state: State,
    host: str = "0.0.0.0",
    samples_per_chunk: int = SAMPLES_PER_CHUNK,
) -> MicStream:
    bytes_per_chunk = samples_per_chunk * WIDTH * CHANNELS

    import socket  # only if needed

    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_socket.bind((host, port))
    _LOGGER.debug("Listening for UDP audio at %s:%s", host, port)

    audio_buffer = bytes()
    is_first_chunk = True

    with udp_socket:
        while True:
            chunk, addr = udp_socket.recvfrom(bytes_per_chunk)
            if state.mic_host is None:
                state.mic_host = addr[0]

            if is_first_chunk:
                _LOGGER.debug("Receiving audio from client")
                is_first_chunk = False

            if audio_buffer or (len(chunk) < bytes_per_chunk):
                # Buffer audio if chunks are too small
                audio_buffer += chunk
                if len(audio_buffer) < bytes_per_chunk:
                    continue

                chunk = audio_buffer[:bytes_per_chunk]
                audio_buffer = audio_buffer[bytes_per_chunk:]

            yield time.monotonic_ns(), chunk


def record_subprocess(
    command: List[str],
    samples_per_chunk: int = SAMPLES_PER_CHUNK,
) -> MicStream:
    """Yield mic samples from a subprocess with a timestamp."""
    _LOGGER.debug("Microphone command: %s", command)
    bytes_per_chunk = samples_per_chunk * WIDTH * CHANNELS
    with subprocess.Popen(command, stdout=subprocess.PIPE) as proc:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(bytes_per_chunk)
            if not chunk:
                break

            yield time.monotonic_ns(), chunk


def record_pulseaudio(
    server: str,
    device: Optional[str],
    samples_per_chunk: int = SAMPLES_PER_CHUNK,
) -> MicStream:
    """Yield mic samples with a timestamp."""

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
        while True:
            chunk = pa.read(samples_per_chunk * WIDTH)
            yield time.monotonic_ns(), chunk
