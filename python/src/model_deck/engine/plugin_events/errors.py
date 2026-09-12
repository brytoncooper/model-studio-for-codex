"""Fixed safe broker errors; messages never echo payloads or identities."""
from __future__ import annotations


class BrokerError(ValueError):
    pass


class BrokerInvalidRequestError(BrokerError):
    def __init__(self) -> None:
        super().__init__("broker invalid request")


class BrokerUnknownTopicError(BrokerError):
    def __init__(self) -> None:
        super().__init__("broker unknown topic")


class BrokerPayloadInvalidError(BrokerError):
    def __init__(self) -> None:
        super().__init__("broker payload invalid")


class BrokerSequenceConflictError(BrokerError):
    def __init__(self) -> None:
        super().__init__("broker sequence conflict")


class BrokerUnknownSubscriptionError(BrokerError):
    def __init__(self) -> None:
        super().__init__("broker unknown subscription")


class BrokerSubscriptionTerminatedError(BrokerError):
    def __init__(self) -> None:
        super().__init__("broker subscription terminated")


class BrokerAckRangeError(BrokerError):
    def __init__(self) -> None:
        super().__init__("broker ack beyond delivered")


class BrokerDeniedError(PermissionError):
    def __init__(self) -> None:
        super().__init__("broker authority denied")
