import logging
import socket
import subprocess
import wave
from typing import Final, List

from .state import State

DEFAULT_APLAY: Final = "aplay -r {rate} -c 1 -f S16_LE -t raw"
APLAY_WITH_DEVICE: Final = "aplay -D {device} -r {rate} -c 1 -f S16_LE -t raw"

_LOGGER = logging.getLogger()


def play_udp(
    media: str,
    udp_socket: socket.socket,
    udp_port: int,
    state: State,
    sample_rate: int,
    samples_per_chunk: int = 1024,
    volume: float = 1.0,
) -> None:
    """Uses ffmpeg to stream raw audio to a UDP port."""
    assert state.mic_host is not None

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
                udp_socket.sendto(chunk, (state.mic_host, udp_port))
                chunk = wav_file.readframes(samples_per_chunk)


def play_subprocess(
    media: str,
    command: List[str],
    sample_rate: int,
    samples_per_chunk: int = 1024,
    volume: float = 1.0,
) -> None:
    """Uses ffmpeg and a subprocess to play a URL to an audio output device."""
    ffmpeg_cmd = [
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
    _LOGGER.debug("play ffmpeg: %s", ffmpeg_cmd)
    _LOGGER.debug("play: %s", command)

    with subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as ffmpeg_proc, subprocess.Popen(command, stdin=subprocess.PIPE) as snd_proc:
        assert ffmpeg_proc.stdout is not None
        assert snd_proc.stdin is not None

        with wave.open(ffmpeg_proc.stdout, "rb") as wav_file:
            assert wav_file.getsampwidth() == 2
            chunk = wav_file.readframes(samples_per_chunk)
            while chunk:
                snd_proc.stdin.write(chunk)
                chunk = wav_file.readframes(samples_per_chunk)
