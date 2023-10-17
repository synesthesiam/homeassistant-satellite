from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


class MicState(int, Enum):
    NOT_RECORDING = auto()
    WAIT_FOR_VAD = auto()
    RECORDING = auto()

    def next(self):
        return MicState(self.value + 1)


@dataclass
class State:
    is_running: bool = True
    mic: MicState = MicState.NOT_RECORDING
    mic_host: Optional[str] = None
    last_event: Optional[str] = None
    vad_prob: float = 0
    pipeline_count: int = 0
