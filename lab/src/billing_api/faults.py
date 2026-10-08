from enum import StrEnum


class Fault(StrEnum):
    PAYMENT_PARTIAL_COMMIT = "payment_partial_commit"


class InjectedFault(RuntimeError):
    pass
