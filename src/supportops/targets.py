from dataclasses import dataclass


@dataclass(frozen=True)
class TargetProfile:
    name: str
    liveness_path: str
    readiness_path: str
    account_path: str
    auth_scheme: str
    lab_writes_allowed: bool


BILLING = TargetProfile(
    name="billing",
    liveness_path="/health",
    readiness_path="/health/ready",
    account_path="/v1/account",
    auth_scheme="Bearer",
    lab_writes_allowed=True,
)

TARGETS = {profile.name: profile for profile in (BILLING,)}


def get_target(name: str) -> TargetProfile:
    return TARGETS[name]
