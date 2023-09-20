import logging
import socket
import time
from typing import Final, Iterable, Optional, Tuple, Union

import sounddevice as sd

from .state import State

RATE: Final = 16000
WIDTH: Final = 2
CHANNELS: Final = 1
SAMPLES_PER_CHUNK = int(0.03 * RATE)  # 30ms

_LOGGER = logging.getLogger()


def record_stream(
    device: Optional[Union[str, int]],
    samples_per_chunk: int = SAMPLES_PER_CHUNK,
) -> Iterable[Tuple[int, bytes]]:
    """Yield mic samples with a timestamp."""
    with sd.RawInputStream(
        device=device,
        samplerate=RATE,
        channels=CHANNELS,
        blocksize=samples_per_chunk,
        dtype="int16",
    ) as stream:
        while True:
            chunk, _overflowed = stream.read(samples_per_chunk)
            chunk = bytes(chunk)
            yield time.monotonic_ns(), chunk


def record_udp(
    port: int,
    state: State,
    host: str = "0.0.0.0",
    samples_per_chunk: int = SAMPLES_PER_CHUNK,
) -> Iterable[Tuple[int, bytes]]:
    bytes_per_chunk = samples_per_chunk * WIDTH

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
