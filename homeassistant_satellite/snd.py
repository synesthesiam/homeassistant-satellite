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


def duck_fail(enable: bool):
    raise Exception("ducking not supported")


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

        yield play, duck_fail


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

    yield play, duck_fail


@contextlib.contextmanager
def play_pulseaudio(
    server: str,
    device: str | None,
    volume: float = 1.0,
    ducking_volume: float = 0.2,
):
    """Uses ffmpeg and pulseaudio to play a URL to an audio output device."""

    import pasimple  # only if
    import pulsectl  # needed

    sample_rate = 44100
    server_name = server if server != "__default__" else None
    app_name = "homeassistant_satellite"

    with pasimple.PaSimple(
        direction=pasimple.PA_STREAM_PLAYBACK,
        server_name=server_name,
        device_name=device,
        app_name=app_name,
        format=pasimple.PA_SAMPLE_S16LE,
        channels=1,
        rate=sample_rate,
    ) as pa, pulsectl.Pulse(server=server_name) as pulse:
        # find the sink we're using
        if device:
            sink = pulse.get_sink_by_name(device)
        else:
            server_info = pulse.server_info()
            sink = pulse.get_sink_by_name(server_info.default_sink_name)

        # set the volume of our own input stream
        for input in pulse.sink_input_list():
            if input.name == app_name:
                pulse.volume_set_all_chans(input, volume)
                break

        orig_volume = {}  # remember original volume when ducking

        def play(media: str):
            with contextlib.closing(
                media_to_chunks(
                    media=media,
                    sample_rate=sample_rate,
                )
            ) as chunks:
                for chunk in chunks:
                    pa.write(chunk)
                pa.drain()

        def duck(enable: bool):
            for input in pulse.sink_input_list():
                # we process all inputs of our sink, except our own input
                if input.sink == sink.index and input.name != app_name:
                    if enable:
                        orig_volume[input.index] = pulsectl.PulseVolumeInfo(
                            input.volume.values
                        )
                        pulse.volume_set_all_chans(input, ducking_volume)

                    elif input.index in orig_volume:
                        pulse.sink_input_volume_set(
                            index=input.index, vol=orig_volume.pop(input.index)
                        )

        yield play, duck


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
