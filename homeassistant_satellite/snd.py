import contextlib
import logging
import subprocess
import wave
from time import sleep
from typing import TYPE_CHECKING, Any, Dict, Final, Generator, List, Optional

from .mic import APP_NAME
from .state import State

DEFAULT_APLAY: Final = "aplay -r {rate} -c 1 -f S16_LE -t raw"
APLAY_WITH_DEVICE: Final = "aplay -D {device} -r {rate} -c 1 -f S16_LE -t raw"

# for typing optional requirements
if TYPE_CHECKING:
    import pulsectl

_LOGGER = logging.getLogger()


def duck_fail(enable: bool):
    raise Exception("ducking not supported")


@contextlib.contextmanager
def play_miniaudio(
    sample_rate: int,
    volume: float = 1.0,
):
    """Uses pyminiaudio to play a URL to an audio output device."""
    try:
        import miniaudio
    except ImportError:
        _LOGGER.fatal("Please pip install homeassistant_satellite[miniaudio]")
        raise
    
    device = miniaudio.PlaybackDevice(
        sample_rate=sample_rate,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1
    )
    
    def play(media: str):
        with contextlib.closing(
            media_to_chunks(media=media, sample_rate=sample_rate, volume=volume)
        ) as chunks:
            for chunk in chunks:
                device.write(chunk)

    try:
        yield play, duck_fail
    finally:
        device.close()


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
def _pulseaudio_echo_cancel(
    enabled: bool,
    snd_device: Optional[str],
    mic_device: Optional[str],
    pactl: "pulsectl.Pulse",
) -> Generator[Any, None, None]:
    """
    Load pulseaudio's module-echo-cancel (if enabled) and return the output sink. Unload on exit.
    """

    srv_info: Any = pactl.server_info()

    sink: Any = pactl.get_sink_by_name(snd_device or srv_info.default_sink_name)
    source: Any = pactl.get_source_by_name(mic_device or srv_info.default_source_name)

    if not enabled:
        yield sink
        return

    ec_module = None
    ec_sink: Any = None
    try:
        # load the module
        args = f"sink_master={sink.name} source_master={source.name} aec_method=webrtc use_master_format=1"
        _LOGGER.debug("loading module-echo-cancel args=%s", args)
        ec_module = pactl.module_load("module-echo-cancel", args=args)

        # find the virtual sink and source created by the module (via the owner_module attribute).
        # In PipeWire owner_module is wrong (bug?) so we fallback to the one with the max index.
        def find(sink_src_list: Any, is_source: bool):
            max_sink_src = None
            for sink_src in sink_src_list:
                if is_source and sink_src.monitor_of_sink not in [None, 4294967295]:
                    continue  # ignore monitor sources
                if sink_src.owner_module == ec_module:
                    return sink_src
                if max_sink_src is None or max_sink_src.index < sink_src.index:
                    max_sink_src = sink_src
            return max_sink_src  # fallback to the sink/source with max index

        ec_sink = find(pactl.sink_list(), False)
        ec_source = find(pactl.source_list(), True)

        # streams already connected to sink should be moved to ec_sink to get
        # echo cancelled (this might happen automatically, depending on pulse
        # config, but we do it ourselves anyway).
        for stream in pactl.sink_input_list():
            if stream.sink == sink.index and stream.owner_module != ec_module:
                _LOGGER.debug("moving stream to %s: %s", ec_sink.name, stream.name)
                pactl.sink_input_move(stream.index, ec_sink.index)

        # the usual is to echo cancel the default sink, in which case we set ec_sink as the default
        # so that future streams are properly echo cancelled.
        if srv_info.default_sink_name == sink.name:
            pactl.default_set(ec_sink)
        else:
            _LOGGER.warning(
                "using non-default device with --echo-cancel. Ensure that all apps send audio to %s",
                ec_sink.name,
            )

        # we finally need to use the virtual source for recording. There is a
        # race condition between this thread that needs to create the virtual
        # source, and the mic thread using it. A simple solution is to just wait
        # until the recording stream is created, and then move it to ec_source.
        def recording_stream():
            while True:
                for stream in pactl.source_output_list():
                    if stream.name == APP_NAME:
                        return stream
                sleep(0.1)

        pactl.source_output_move(recording_stream().index, ec_source.index)

        yield ec_sink

    finally:
        # unload module and ensure that streams are move back to the original sink (if they are not moved automatically)
        to_restore = {
            s.index for s in pactl.sink_input_list() if s.sink == ec_sink.index
        }

        pactl.module_unload(ec_module)

        for stream in pactl.sink_input_list():
            if stream.index in to_restore and stream.sink != sink.index:
                _LOGGER.debug("moving %s back to %s", stream.name, sink.name)
                pactl.sink_input_move(stream.index, sink.index)

        # restore the default sink
        if srv_info.default_sink_name == sink.name:
            pactl.default_set(sink)


@contextlib.contextmanager
def play_pulseaudio(
    server: str,
    snd_device: Optional[str],
    mic_device: Optional[str],
    volume: float = 1.0,
    ducking_volume: float = 0.2,
    echo_cancel: bool = False,
):
    """Uses ffmpeg and pulseaudio to play a URL to an audio output device."""

    try:
        import pasimple
        import pulsectl
    except ImportError:
        _LOGGER.fatal("Please pip install homeassistant_satellite[pulseaudio]")
        raise

    # Open the pulsectl connection (for controlling the server) and setup echo
    # cancel. The playback stream (pasimple) opens only when playing media.

    server_name = server if server != "__default__" else None

    with pulsectl.Pulse(server=server_name) as pactl, _pulseaudio_echo_cancel(
        enabled=echo_cancel,
        pactl=pactl,
        snd_device=snd_device,
        mic_device=mic_device,
    ) as sink:
        ducked: Dict[int, float] = {}  # stream index => volume before ducking

        def play(media: str):
            # The playback rate can be arbitrary since ffmpeg resamples to that
            # rate. In the future we could remove ffmpeg by opening directly the
            # wav file and setting the sample rate to that of the wav data.
            sample_rate = 22050

            with pasimple.PaSimple(
                direction=pasimple.PA_STREAM_PLAYBACK,
                server_name=server_name,
                device_name=sink.name,
                app_name=APP_NAME,
                format=pasimple.PA_SAMPLE_S16LE,
                channels=1,
                rate=sample_rate,
            ) as pa, contextlib.closing(
                media_to_chunks(
                    media=media,
                    sample_rate=sample_rate,
                )
            ) as chunks:
                # set the volume of our own playback stream
                stream = next(s for s in pactl.sink_input_list() if s.name == APP_NAME)
                pactl.volume_set_all_chans(stream, volume)

                for chunk in chunks:
                    pa.write(chunk)
                pa.drain()

        def duck(enable: bool):
            for stream in pactl.sink_input_list():
                # we process all inputs of our sink, except our own input
                if stream.sink == sink.index and stream.name != APP_NAME:
                    if enable:
                        ducked.setdefault(  # don't update if already ducked
                            stream.index, pulsectl.PulseVolumeInfo(stream.volume.values)
                        )
                        pactl.volume_set_all_chans(stream, ducking_volume)

                    elif stream.index in ducked:
                        pactl.sink_input_volume_set(
                            index=stream.index, vol=ducked.pop(stream.index)
                        )

        try:
            yield play, duck
        finally:
            # unduck on exit
            for ducked_index, ducked_volume in ducked.items():
                pactl.sink_input_volume_set(index=ducked_index, vol=ducked_volume)


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
