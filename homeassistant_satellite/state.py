from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


class MicState(str, Enum):
    NOT_RECORDING = auto()
    WAIT_FOR_VAD = auto()
    RECORDING = auto()


@dataclass
class State:
    is_running: bool = True
    mic: MicState = MicState.NOT_RECORDING
    mic_host: Optional[str] = None
