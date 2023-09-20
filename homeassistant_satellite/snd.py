import logging
import socket
import subprocess
import wave

import sounddevice as sd

from .state import State

_LOGGER = logging.getLogger()


def play_stream(
    media: str,
    stream: sd.RawOutputStream,
    sample_rate: int,
    samples_per_chunk: int = 1024,
    volume: float = 1.0,
) -> None:
    """Uses ffmpeg and sounddevice to play a URL to an audio output device."""
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
                stream.write(chunk)
                chunk = wav_file.readframes(samples_per_chunk)


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
