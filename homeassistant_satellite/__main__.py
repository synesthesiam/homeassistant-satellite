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
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Final, Optional, Tuple, Union

from .mic import (
    ARECORD_WITH_DEVICE,
    CHANNELS,
    DEFAULT_ARECORD,
    RATE,
    SAMPLES_PER_CHUNK,
    WIDTH,
    record_pulseaudio,
    record_subprocess,
    record_udp,
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
from .util import multiply_volume
from .vad import SileroVoiceActivityDetector, VoiceActivityDetector


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

VAD_DISABLED = "disabled"
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
    speech_detected = asyncio.Event()
    state = State(mic=MicState.WAIT_FOR_VAD)

    # Recording thread for microphone
    mic_thread = threading.Thread(
        target=_mic_proc,
        args=(args, loop, recording_queue, speech_detected, state),
        daemon=True,
    )
    mic_thread.start()

    # Playback thread
    playback_thread = threading.Thread(
        target=_playback_proc,
        args=(args, playback_queue, state),
        daemon=True,
    )
    playback_thread.start()

    # Send requests to the playback thread
    try:
        while state.is_running:
            try:
                if args.vad != VAD_DISABLED:
                    _LOGGER.debug("Waiting for speech")
                    await speech_detected.wait()
                    speech_detected.clear()

                    if not state.is_running:
                        # Error in mic thread
                        break

                    _LOGGER.debug("Speech detected")

                async for _timestamp, event_type, event_data in stream(
                    host=args.host,
                    protocol=args.protocol,
                    port=args.port,
                    token=token,
                    audio=recording_queue,
                    pipeline_name=args.pipeline,
                    audio_seconds_to_buffer=args.wake_buffer_seconds,
                ):
                    _LOGGER.debug("%s %s", event_type, event_data)

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

            except Exception:
                _LOGGER.exception("Unexpected error")
                state.mic = MicState.WAIT_FOR_VAD
    finally:
        state.is_running = False
        mic_thread.join()
        playback_queue.put_nowait(None)  # exit request
        playback_thread.join()


# -----------------------------------------------------------------------------


def _mic_proc(
    args: argparse.Namespace,
    loop: asyncio.AbstractEventLoop,
    audio_queue: "asyncio.Queue[Tuple[int, bytes]]",
    speech_detected: asyncio.Event,
    state: State,
) -> None:
    try:
        audio_processor: "Optional[AudioProcessor]" = None
        vad: Optional[VoiceActivityDetector] = None
        vad_activation: int = 0
        vad_chunk_buffer: Deque[Tuple[int, bytes]] = deque(
            maxlen=args.vad_buffer_chunks
        )
        sub_chunk_samples: Final = 160
        sub_chunk_bytes: Final = sub_chunk_samples * 2  # 16-bit
        wav_writer: Optional[wave.Wave_write] = None

        if (
            (args.vad == "webrtcvad")
            or (args.noise_suppression > 0)
            or (args.auto_gain > 0)
        ):
            from webrtc_noise_gain import AudioProcessor

            audio_processor = AudioProcessor(args.auto_gain, args.noise_suppression)

            # Required so we don't need an extra buffer
            assert (
                SAMPLES_PER_CHUNK % sub_chunk_samples
            ) == 0, "Audio chunks must be a multiple of 10ms"
            _LOGGER.debug("Using webrtc audio processing")

        if args.vad == "silero":
            vad = SileroVoiceActivityDetector(args.vad_model)
            _LOGGER.debug("Using silero VAD")

        if args.udp_mic is not None:
            # UDP socket
            mic_stream = record_udp(args.udp_mic, state)
        elif args.pulseaudio is not None:
            # PulseAudio
            mic_stream = record_pulseaudio(args.pulseaudio, args.mic_device)
        else:
            # External program
            mic_stream = record_subprocess(args.mic_command)

        for ts_chunk in mic_stream:
            if not state.is_running:
                break

            timestamp, chunk = ts_chunk
            vad_prob = 0.0

            if args.volume_multiplier != 1.0:
                chunk = multiply_volume(chunk, args.volume_multiplier)

            # Process in 10ms sub-chunks.
            if audio_processor is not None:
                clean_chunk = bytes()
                ap_sub_chunks = len(chunk) // sub_chunk_bytes
                if (len(chunk) % sub_chunk_bytes) != 0:
                    _LOGGER.warning("Mic chunk size is not a multiple of 10ms")

                for sub_chunk_idx in range(ap_sub_chunks):
                    sub_chunk_offset = sub_chunk_idx * sub_chunk_bytes
                    result = audio_processor.Process10ms(
                        chunk[sub_chunk_offset : (sub_chunk_offset + sub_chunk_bytes)]
                    )

                    clean_chunk += result.audio
                    if result.is_speech:
                        vad_prob = 1.0

                # Overwrite with clean audio
                chunk = clean_chunk
                ts_chunk = (timestamp, clean_chunk)

            if state.mic == MicState.WAIT_FOR_VAD:
                if wav_writer is not None:
                    wav_writer.close()
                    wav_writer = None

                if (vad is None) and (audio_processor is None):
                    # No VAD
                    state.mic = MicState.RECORDING
                else:
                    if vad is not None:
                        # silero
                        vad_prob = vad(chunk)

                    if vad_prob >= args.vad_threshold:
                        vad_activation += 1
                    else:
                        vad_activation = max(0, vad_activation - 1)

                    if vad_activation >= args.vad_trigger_level:
                        state.mic = MicState.RECORDING
                        speech_detected.set()

                        if vad is not None:
                            vad.reset()

                        vad_activation = 0
                    else:
                        vad_chunk_buffer.append(ts_chunk)

            if state.mic == MicState.RECORDING:
                if args.debug_recording_dir and (wav_writer is None):
                    # Save audio
                    wav_writer = wave.open(
                        str(args.debug_recording_dir / f"{time.monotonic_ns()}.wav"),
                        "wb",
                    )
                    wav_writer.setframerate(RATE)
                    wav_writer.setsampwidth(WIDTH)
                    wav_writer.setnchannels(CHANNELS)

                if vad_chunk_buffer:
                    for buffered_chunk in vad_chunk_buffer:
                        loop.call_soon_threadsafe(
                            audio_queue.put_nowait, buffered_chunk
                        )

                        if wav_writer is not None:
                            wav_writer.writeframes(buffered_chunk[1])

                    vad_chunk_buffer.clear()

                loop.call_soon_threadsafe(audio_queue.put_nowait, ts_chunk)
                if wav_writer is not None:
                    wav_writer.writeframes(ts_chunk[1])

    except Exception:
        _LOGGER.exception("Unexpected error in _mic_proc")
        os._exit(-1)  # pylint: disable=protected-access


# ----------------------------


def _playback_proc(
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
                    play(item.media)

                elif isinstance(item, SetMicState):
                    state.mic = item.mic_state

                elif isinstance(item, Duck):
                    duck(item.enable)

            return  # we got None from the queue, exit

    except Exception:
        _LOGGER.exception("Sound error in _playback_proc")
        os._exit(-1)  # pylint: disable=protected-access


# -----------------------------------------------------------------------------


def run():
    asyncio.run(main())



if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
