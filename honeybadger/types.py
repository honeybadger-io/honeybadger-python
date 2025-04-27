from enum import Enum
from dataclasses import dataclass
from typing import Optional, Protocol, Any, Dict


class EventsSendStatus(Enum):
    OK = "ok"
    THROTTLING = "throttling"
    ERROR = "error"


@dataclass(frozen=True)
class EventsSendResult:
    status: EventsSendStatus
    reason: Optional[str] = None


Notice = Dict[str, Any]
Event = Dict[str, Any]
