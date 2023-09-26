import contextlib
import logging
import subprocess
from typing import Generator
import wave
from typing import Final, List

from .state import State

DEFAULT_APLAY: Final = "aplay -r {rate} -c 1 -f S16_LE -t raw"
APLAY_WITH_DEVICE: Final = "aplay -D {device} -r {rate} -c 1 -f S16_LE -t raw"

_LOGGER = logging.getLogger()


@contextlib.contextmanager
def play_udp(
    udp_port: int,
    state: State,
    sample_rate: int,
    volume: float = 1.0,
):
    """Uses ffmpeg to stream raw audio to a UDP port."""
    assert state.mic_host is not None

    import socket  # only if needed

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:

        def play(media: str):
            with contextlib.closing(
                media_to_chunks(media=media, sample_rate=sample_rate, volume=volume)
            ) as chunks:
                for chunk in chunks:
                    udp_socket.sendto(chunk, (state.mic_host, udp_port))

        yield play


@contextlib.contextmanager
def play_subprocess(
    command: List[str],
    sample_rate: int,
    volume: float = 1.0,
):
    """Uses ffmpeg and a subprocess to play a URL to an audio output device."""
    _LOGGER.debug("play: %s", command)

    def play(media: str):
        # Spawn a new subprocess each time we play a sound
        with subprocess.Popen(
            command, stdin=subprocess.PIPE
        ) as snd_proc, contextlib.closing(
            media_to_chunks(media=media, sample_rate=sample_rate, volume=volume)
        ) as chunks:
            assert snd_proc.stdin is not None
            for chunk in chunks:
                snd_proc.stdin.write(chunk)

    yield play


def media_to_chunks(
    media: str,
    sample_rate: int,
    samples_per_chunk: int = 1024,
    volume: float = 1.0,
) -> Generator[bytes, None, None]:
    cmd = [
        "ffmpeg",
        "-i",
        media,
        "-f",
        "wav",
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        "-filter:a",
        f"volume={volume}",
        "-",
    ]
    _LOGGER.debug("play: %s", cmd)

    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as proc:
        assert proc.stdout is not None
        with wave.open(proc.stdout, "rb") as wav_file:
            assert wav_file.getsampwidth() == 2
            chunk = wav_file.readframes(samples_per_chunk)
            while chunk:
                yield chunk
                chunk = wav_file.readframes(samples_per_chunk)
