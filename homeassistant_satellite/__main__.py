#!/usr/bin/env python3
import argparse
import asyncio
import logging
import os
import queue
import shlex
import shutil
import sys
import threading
import time
import wave
import socket
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Union

from homeassistant_satellite.ha_connection import HAConnection

from .mic_record import (
    ARECORD_WITH_DEVICE,
    DEFAULT_ARECORD,
)
from .mic_process import (
    VAD_DISABLED,
    WAKE_WORD_DISABLED,
    mic_thread_entry,
)
from .remote import stream
from .snd import (
    APLAY_WITH_DEVICE,
    DEFAULT_APLAY,
    play_pulseaudio,
    play_subprocess,
    play_udp,
)
from .state import MicState, State


# items of the playback queue
@dataclass
class PlayMedia:
    media: str  # wav url/path to play


@dataclass
class SetMicState:
    mic_state: MicState  # mic state after playing


@dataclass
class Duck:
    enable: bool  # enable/disable ducking


PlaybackQueueItem = Optional[Union[PlayMedia, SetMicState, Duck]]

_LOGGER = logging.getLogger(__name__)
_DIR = Path(__file__).parent


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, help="Home Assistant server host")
    token_group = parser.add_mutually_exclusive_group(required=True)
    token_group.add_argument("--token", help="Long-lived access token")
    token_group.add_argument(
        "--token-file", help="Path to a file containing the Long-lived access token"
    )
    parser.add_argument(
        "--port", type=int, help="Port for Home Assistant server", default=8123
    )
    parser.add_argument("--api-path", default="/api")
    parser.add_argument("--pipeline", help="Name of pipeline")
    parser.add_argument(
        "--protocol",
        default="http",
        choices=("http", "https"),
        help="Home Assistant protocol",
    )
    #
    parser.add_argument("--mic-device", help="Name of ALSA microphone device")
    parser.add_argument("--snd-device", help="Name of ALSA sound device")
    #
    parser.add_argument(
        "--mic-command",
        help="External command to run for raw audio data (16Khz, 16-bit mono PCM)",
    )
    parser.add_argument(
        "--snd-command",
        help="External command to run for raw audio out (see --snd-command-sample-rate)",
    )
    parser.add_argument(
        "--snd-command-sample-rate",
        type=int,
        default=22050,
        help="Sample rate for --snd-command (default: 22050)",
    )
    #
    parser.add_argument(
        "--awake-sound", help="Audio file to play when wake word is detected"
    )
    parser.add_argument(
        "--done-sound", help="Audio file to play when voice command is done"
    )
    parser.add_argument(
        "--volume", type=float, default=1.0, help="Playback volume (0-1)"
    )
    #
    parser.add_argument(
        "--vad", choices=(VAD_DISABLED, "webrtcvad", "silero"), default=VAD_DISABLED
    )
    parser.add_argument(
        "--vad-model",
        help="Path to Silero VAD onnx model (v4)",
        default=_DIR / "models" / "silero_vad.onnx",
    )
    parser.add_argument("--vad-threshold", type=float, default=0.5)
    parser.add_argument("--vad-trigger-level", type=int, default=3)
    parser.add_argument("--vad-buffer-chunks", type=int, default=40)
    #
    parser.add_argument("--wake-buffer-seconds", type=float, default=0)
    #
    parser.add_argument(
        "--noise-suppression", type=int, default=0, choices=(0, 1, 2, 3, 4)
    )
    parser.add_argument("--auto-gain", type=int, default=0, choices=list(range(32)))
    parser.add_argument("--volume-multiplier", type=float, default=1.0)
    #
    parser.add_argument(
        "--wake-word",
        choices=(WAKE_WORD_DISABLED, "wyoming"),
        default=WAKE_WORD_DISABLED,
    )
    parser.add_argument("--wake-word-id", type=str)
    parser.add_argument("--wyoming-host", type=str, default="localhost")
    parser.add_argument("--wyoming-port", type=int, default=10400)
    #
    parser.add_argument(
        "--pulseaudio",
        nargs="?",
        const="__default__",  # when used without argument
        help="Use pulseaudio (socket/hostname optionally passed as argument)",
    )
    parser.add_argument(
        "--ducking-volume",
        type=float,
        help="Set output volume to this value while recording",
    )
    parser.add_argument(
        "--echo-cancel",
        action="store_true",
        help="Enable acoustic echo cancellation",
    )
    #
    parser.add_argument("--udp-mic", type=int, help="UDP port to receive input audio")
    parser.add_argument("--udp-snd", type=int, help="UDP port to send output audio")
    parser.add_argument(
        "--udp-snd-sample-rate",
        type=int,
        default=22050,
        help="Sample rate for UDP output audio (default: 22050)",
    )
    #
    parser.add_argument(
        "--debug-recording-dir", help="Directory to store audio for debugging"
    )
    #
    parser.add_argument(
        "--debug", action="store_true", help="Print DEBUG messages to the console"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    _LOGGER.debug(args)

    if not shutil.which("ffmpeg"):
        _LOGGER.fatal("Please install ffmpeg")
        sys.exit(1)

    if args.token_file:
        with open(args.token_file, "r", encoding="utf-8") as fd:
            token = fd.read().strip()
    else:
        token = args.token

    if args.mic_device and (not args.mic_command):
        args.mic_command = ARECORD_WITH_DEVICE.format(device=args.mic_device)

    if not args.mic_command:
        args.mic_command = DEFAULT_ARECORD

    if args.snd_device and (not args.snd_command):
        args.snd_command = APLAY_WITH_DEVICE.format(
            device=args.snd_device, rate=args.snd_command_sample_rate
        )

    if not args.snd_command:
        args.snd_command = DEFAULT_APLAY.format(rate=args.snd_command_sample_rate)

    args.mic_command = shlex.split(args.mic_command)
    args.snd_command = shlex.split(args.snd_command)

    if args.awake_sound and not os.path.isfile(args.awake_sound):
        _LOGGER.fatal("--awake-sound points to non-existing file")
        sys.exit(1)

    if args.done_sound and not os.path.isfile(args.done_sound):
        _LOGGER.fatal("--done-sound points to non-existing file")
        sys.exit(1)

    if args.ducking_volume and not args.pulseaudio:
        _LOGGER.fatal("--ducking-volume only available with --pulseaudio")
        sys.exit(1)
    if args.echo_cancel and not args.pulseaudio:
        _LOGGER.fatal("--echo-cancel only available with --pulseaudio")
        sys.exit(1)

    if args.debug_recording_dir:
        # Create directory for saving debug audio
        args.debug_recording_dir = Path(args.debug_recording_dir)
        args.debug_recording_dir.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    recording_queue: "asyncio.Queue[Tuple[int, bytes]]" = asyncio.Queue()
    playback_queue: "queue.Queue[PlaybackQueueItem]" = queue.Queue()
    ready_to_stream = asyncio.Event()
    state = State(mic=MicState.WAIT_FOR_VAD)

    # Recording thread for microphone
    mic_thread = threading.Thread(
        target=mic_thread_entry,
        args=(args, loop, recording_queue, ready_to_stream, state),
        daemon=True,
    )
    mic_thread.start()

    # Playback thread
    playback_thread = threading.Thread(
        target=_playback_thread_entry,
        args=(args, playback_queue, state),
        daemon=True,
    )
    playback_thread.start()

    # Connect to HA and continuously run pipelines
    try:
        async with HAConnection(
            host=args.host,
            protocol=args.protocol,
            port=args.port,
            token=token,
        ) as ha_connection:
            while state.is_running:
                await _run_pipeline(
                    args=args,
                    state=state,
                    ha_connection=ha_connection,
                    ready_to_stream=ready_to_stream,
                    recording_queue=recording_queue,
                    playback_queue=playback_queue,
                )

    except Exception:
        _LOGGER.exception("Unknown exception in the main thread")

    finally:
        state.is_running = False
        mic_thread.join()
        playback_queue.put_nowait(None)  # exit request
        playback_thread.join()


async def _run_pipeline(
    args,
    state: State,
    ha_connection: HAConnection,
    ready_to_stream: asyncio.Event,
    recording_queue: "asyncio.Queue[Tuple[int, bytes]]",
    playback_queue: "queue.Queue[PlaybackQueueItem]",
):
    """Run a single pipeline in the main thread"""

    state.pipeline_count += 1

    if args.vad != VAD_DISABLED:
        _LOGGER.debug("Waiting for speech")

    # The ready_to_stream event fires when local processing is over and we are
    # ready to stream audio to HA.
    await ready_to_stream.wait()
    ready_to_stream.clear()

    if not state.is_running:
        # Error in mic thread
        return

    async for _timestamp, event_type, event_data in stream(
        ha_connection=ha_connection,
        audio=recording_queue,
        pipeline_name=args.pipeline,
        audio_seconds_to_buffer=args.wake_buffer_seconds,
        start_stage=("wake_word" if args.wake_word == WAKE_WORD_DISABLED else "stt"),
    ):
        _LOGGER.warning("%s %s", event_type, event_data)

        if event_type == "wake_word-end":
            if args.ducking_volume is not None:
                playback_queue.put_nowait(Duck(True))

            if args.awake_sound:
                state.mic = MicState.NOT_RECORDING
                playback_queue.put_nowait(PlayMedia(args.awake_sound))
                playback_queue.put_nowait(SetMicState(MicState.RECORDING))

        elif event_type == "stt-end":
            # Stop recording until run ends
            state.mic = MicState.NOT_RECORDING
            if args.done_sound:
                playback_queue.put_nowait(PlayMedia(args.done_sound))

        elif event_type == "tts-end":
            # Play TTS output
            tts_url = event_data.get("tts_output", {}).get("url")
            if tts_url:
                url = f"{args.protocol}://{args.host}:{args.port}{tts_url}"
                playback_queue.put_nowait(PlayMedia(url))

        elif event_type in ("run-end", "error"):
            # Start recording for next wake word (after TTS finishes)
            playback_queue.put_nowait(SetMicState(MicState.WAIT_FOR_VAD))

            if args.ducking_volume is not None:
                playback_queue.put_nowait(Duck(False))

        # For the main events that change the state of the satellite, fire a HA
        # event to let the world know about our state. Skip consecutive same
        # events (mainly consecutive run-ends).
        if (
            event_type in ["wake_word-end", "stt-end", "tts-end", "run-end"]
            and event_type != state.last_event
        ):
            state.last_event = event_type
            asyncio.create_task(  # in background
                ha_connection.send_and_receive(
                    {
                        "type": "fire_event",
                        "event_type": "homeassistant_satellite_event",
                        "event_data": {
                            "satellite_name": socket.gethostname(),
                            "pipeline_event": {
                                "type": event_type,
                                "data": event_data,
                            },
                        },
                    }
                )
            )


# -----------------------------------------------------------------------------


def _playback_thread_entry(
    args: argparse.Namespace,
    playback_queue: "queue.Queue[PlaybackQueueItem]",
    state: State,
) -> None:
    try:
        if args.udp_snd is not None:
            # UDP socket
            play_ctx = play_udp(
                udp_port=args.udp_snd,
                state=state,
                sample_rate=args.udp_snd_sample_rate,
                volume=args.volume,
            )
        elif args.pulseaudio is not None:
            # PulseAudio
            play_ctx = play_pulseaudio(
                server=args.pulseaudio,
                snd_device=args.snd_device,
                mic_device=args.mic_device,
                volume=args.volume,
                ducking_volume=args.ducking_volume,
                echo_cancel=args.echo_cancel,
            )
        else:
            # External program
            play_ctx = play_subprocess(
                command=args.snd_command,
                sample_rate=args.snd_command_sample_rate,
                volume=args.volume,
            )

        with play_ctx as (play, duck):
            for item in iter(playback_queue.get, None):
                if isinstance(item, PlayMedia):
                    try:
                        play(item.media)
                    except EOFError:
                        pass  # expected when media item is empty ("never mind")
                    except Exception:
                        _LOGGER.exception(
                            "Unexpected error playing media item: %s", item
                        )

                elif isinstance(item, SetMicState):
                    state.mic = item.mic_state

                elif isinstance(item, Duck):
                    duck(item.enable)

            return  # we got None from the queue, exit

    except Exception:
        _LOGGER.exception("Sound error in _playback_thread_entry")
        os._exit(-1)  # pylint: disable=protected-access


# -----------------------------------------------------------------------------


def run():
    asyncio.run(main())


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
