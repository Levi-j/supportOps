from dataclasses import dataclass


@dataclass(frozen=True)
class TargetProfile:
    name: str
    liveness_path: str
    readiness_path: str
    account_path: str
    auth_scheme: str
    lab_writes_allowed: bool
    api_key_prefix: str
    api_key_pattern: str
    api_key_prefix_length: int


BILLING = TargetProfile(
    name="billing",
    liveness_path="/health",
    readiness_path="/health/ready",
    account_path="/v1/account",
    auth_scheme="Bearer",
    lab_writes_allowed=True,
    api_key_prefix="bk_",
    api_key_pattern=r"bk_[A-Za-z0-9_]{17,125}",
    api_key_prefix_length=12,
)

TARGETS = {profile.name: profile for profile in (BILLING,)}


def get_target(name: str) -> TargetProfile:
    return TARGETS[name]
