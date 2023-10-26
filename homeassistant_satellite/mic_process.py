"""
This module runs in the mic thread and is responsible for processing the chunks
recorded from the microphone.
"""

import argparse
import asyncio
import logging
import os
import time
import wave
from collections import deque
from pathlib import Path
from typing import Deque, Final, Optional, Tuple

from .mic_record import (
    CHANNELS,
    RATE,
    SAMPLES_PER_CHUNK,
    WIDTH,
    MicStream,
    record_pulseaudio,
    record_subprocess,
    record_udp,
)
from .state import MicState, State
from .util import multiply_volume
from .vad import SileroVoiceActivityDetector
from .wake_word import WyomingWakeWordDetector

VAD_DISABLED = "disabled"
WAKE_WORD_DISABLED = "disabled"

_LOGGER = logging.getLogger()


"""
Mic processing is performed by "piping" several functions that perform
independent work. Each receives a MicStream (ts_chunk iterable) as input and
produces a MicStream in the ouput. Each pipe is allowed to modify chunks, block
them, buffer them, etc. Chunks that exit the whole pipeline are streamed to HA.
"""


def __ensure_running_pipe(
    mic_input: MicStream,
    state: State,
) -> MicStream:
    """Stops the recording pipeline when exiting."""

    for ts_chunk in mic_input:
        if not state.is_running:
            break

        yield ts_chunk


def _volume_multiplier_pipe(
    mic_input: MicStream,
    volume_multiplier: float,
) -> MicStream:
    """Multiplies the volume of all passing chunks."""

    for timestamp, chunk in mic_input:
        chunk = multiply_volume(chunk, volume_multiplier)
        yield timestamp, chunk


def _webrtc_pipe(
    mic_input: MicStream,
    state: State,
    noise_suppression: int,
    auto_gain: int,
    vad_enabled: bool,
) -> MicStream:
    """
    Processes passing chunks with webrtc. If vad_enabled == True it also
    performs VAD for each chunk and stores the result in state.vad_prob.
    """

    from webrtc_noise_gain import AudioProcessor

    audio_processor = AudioProcessor(auto_gain, noise_suppression)

    sub_chunk_samples: Final = 160
    sub_chunk_bytes: Final = sub_chunk_samples * 2  # 16-bit

    # Required so we don't need an extra buffer
    assert (
        SAMPLES_PER_CHUNK % sub_chunk_samples
    ) == 0, "Audio chunks must be a multiple of 10ms"
    _LOGGER.debug("Using webrtc audio processing")

    for timestamp, chunk in mic_input:
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

            if vad_enabled and state.mic == MicState.WAIT_FOR_VAD and result.is_speech:
                state.vad_prob = 1.0

        # return clean audio
        yield timestamp, clean_chunk


def _silero_pipe(
    mic_input: MicStream,
    vad_model: str,
    state: State,
) -> MicStream:
    """Performs silero VAD for each passing chunk and stores the result in state.vad_prob."""

    _LOGGER.debug("Using silero VAD")

    silero = SileroVoiceActivityDetector(vad_model)
    running = False

    for timestamp, chunk in mic_input:
        if state.mic == MicState.WAIT_FOR_VAD:
            running = True
            state.vad_prob = silero(chunk)

        elif running:
            running = False
            silero.reset()

        yield timestamp, chunk


def _vad_pipe(
    mic_input: MicStream,
    vad_threshold: float,
    vad_trigger_level: int,
    vad_buffer_chunks: int,
    state: State,
) -> MicStream:
    """
    Implements VAD logic. During WAIT_FOR_VAD we block and buffer chunks until
    the VAD trigger is reached. For this to work VAD evaluation must have been
    already performed in the input chunks (by _webrtc_pipe and _silero_pipe),
    the result is read from state.vad_prob.

    After triggering, previously buffered chunks as well as all future chunks
    are sent to the output.
    """

    vad_activation: int = 0
    vad_chunk_buffer: Deque[Tuple[int, bytes]] = deque(maxlen=vad_buffer_chunks)

    for ts_chunk in mic_input:
        # If we're not waiting for VAD, just let the chunk pass through
        if state.mic != MicState.WAIT_FOR_VAD:
            yield ts_chunk
            continue

        # We're waiting for VAD, chunks are buffered until we trigger
        vad_chunk_buffer.append(ts_chunk)

        # count activations based on the state.vad_prob already set upstream
        if state.vad_prob >= vad_threshold:
            vad_activation += 1
        else:
            vad_activation = max(0, vad_activation - 1)
        state.vad_prob = 0

        if vad_activation >= vad_trigger_level:
            # Waiting for VAD and just got triggered, set the state and send
            # buffered chunks downstream before continuing with new ones
            _LOGGER.warning("Speech detected")

            state.mic = MicState.WAIT_FOR_WAKE_WORD

            for buffered_chunk in vad_chunk_buffer:
                yield buffered_chunk

            vad_activation = 0
            vad_chunk_buffer.clear()


def _skip_mic_state_pipe(
    mic_input: MicStream,
    state: State,
    skip: MicState,
    event: Optional[asyncio.Event] = None,
):
    """Skips one MicState of the processing pipeline and continuous immediately to the next one.
    If event is not None it is set when the skip occurs.
    """

    for ts_chunk in mic_input:
        if state.mic == skip:
            state.mic = state.mic.next()

            if event is not None:
                event.set()

        yield ts_chunk


def _wyoming_wake_word_pipe(
    mic_input: MicStream,
    state: State,
    wyoming_host: str,
    wyoming_port: int,
    wake_word_id: Optional[str],
    event: asyncio.Event,
    loop: asyncio.AbstractEventLoop,
):
    with WyomingWakeWordDetector(
        host=wyoming_host,
        port=wyoming_port,
        wake_word_id=wake_word_id,
        loop=loop,
    ) as wake:
        for ts_chunk in mic_input:
            # Detections arrive asyncronously, check whether the wake word is
            # already detected before processing the current chunks.
            if state.mic == MicState.WAIT_FOR_WAKE_WORD and wake.detected:
                _LOGGER.warning("Wake word detected")

                wake.reset()
                state.mic = MicState.RECORDING
                event.set()

            if state.mic == MicState.WAIT_FOR_WAKE_WORD:
                wake.process_chunk(ts_chunk)
            else:
                yield ts_chunk


def _wav_writer_pipe(
    mic_input: MicStream,
    state: State,
    debug_recording_dir: Path,
) -> MicStream:
    """Record the chunks passing through us in a wav file."""

    wav_writer: Optional[wave.Wave_write] = None
    current_pipeline = -1

    for timestamp, chunk in mic_input:
        # wav_writer is installed after VAD so all the chunks we see are meant to be recorded.
        # Note that the state might go from RECORDING to RECORDING again in the next pipeline, so
        # we detect that the pipeline changed from state.pipeline_count.

        if current_pipeline != state.pipeline_count:
            if wav_writer is not None:
                wav_writer.close()

            wav_writer = wave.open(
                str(debug_recording_dir / f"{time.monotonic_ns()}.wav"),
                "wb",
            )
            wav_writer.setframerate(RATE)
            wav_writer.setsampwidth(WIDTH)
            wav_writer.setnchannels(CHANNELS)

        assert wav_writer
        wav_writer.writeframes(chunk)

        yield timestamp, chunk


def mic_thread_entry(
    args: argparse.Namespace,
    loop: asyncio.AbstractEventLoop,
    recording_queue: "asyncio.Queue[Tuple[int, bytes]]",
    ready_to_stream: asyncio.Event,
    state: State,
) -> None:
    """
    The entrypoint of the mic thread. Reads chunks from the microphone, pipes them through
    various processing stages and finally outputs them to the recording_queue.
    """

    try:
        # Select our mic source
        if args.udp_mic is not None:
            mic_stream = record_udp(args.udp_mic, state)
        elif args.pulseaudio is not None:
            mic_stream = record_pulseaudio(args.pulseaudio, args.mic_device)
        else:
            mic_stream = record_subprocess(args.mic_command)

        # Then, depending on the given arguments, enable the corresponding pipes
        # to process the mic audio.

        mic_stream = __ensure_running_pipe(mic_input=mic_stream, state=state)

        if args.volume_multiplier != 1.0:
            mic_stream = _volume_multiplier_pipe(
                mic_input=mic_stream, volume_multiplier=args.volume_multiplier
            )

        if args.vad == "webrtcvad" or args.noise_suppression > 0 or args.auto_gain > 0:
            mic_stream = _webrtc_pipe(
                mic_input=mic_stream,
                state=state,
                noise_suppression=args.noise_suppression,
                auto_gain=args.auto_gain,
                vad_enabled=(args.vad == "webrtcvad"),
            )

        if args.vad == "silero":
            mic_stream = _silero_pipe(
                mic_input=mic_stream,
                state=state,
                vad_model=args.vad_model,
            )

        if args.vad != VAD_DISABLED:
            mic_stream = _vad_pipe(
                mic_input=mic_stream,
                state=state,
                vad_threshold=args.vad_threshold,
                vad_trigger_level=args.vad_trigger_level,
                vad_buffer_chunks=args.vad_buffer_chunks,
            )
        else:
            # No vad, skip it
            mic_stream = _skip_mic_state_pipe(
                mic_input=mic_stream,
                state=state,
                skip=MicState.WAIT_FOR_VAD,
            )

        if args.wake_word == "wyoming":
            mic_stream = _wyoming_wake_word_pipe(
                mic_input=mic_stream,
                state=state,
                wyoming_host=args.wyoming_host,
                wyoming_port=args.wyoming_port,
                wake_word_id=args.wake_word_id,
                loop=loop,
                event=ready_to_stream,
            )
        else:
            # No wake word detection, skip it
            mic_stream = _skip_mic_state_pipe(
                mic_input=mic_stream,
                state=state,
                skip=MicState.WAIT_FOR_WAKE_WORD,
                event=ready_to_stream,
            )

        if args.debug_recording_dir:
            mic_stream = _wav_writer_pipe(
                mic_input=mic_stream,
                state=state,
                debug_recording_dir=args.debug_recording_dir,
            )

        # Finally, the chunks passing through all pipes are ready to be streamed
        for ts_chunk in mic_stream:
            loop.call_soon_threadsafe(recording_queue.put_nowait, ts_chunk)

    except Exception:
        _LOGGER.exception("Unexpected error in mic_thread_entry")
        os._exit(-1)  # pylint: disable=protected-access
