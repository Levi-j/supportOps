from dataclasses import dataclass
from typing import Literal

from supportops.errors import ConfigError

CheckPack = Literal["billing", "orderflow"]
HealthStyle = Literal["billing", "actuator"]
GENERIC_PACK = "generic"


@dataclass(frozen=True)
class ApiKeyFormat:
    prefix: str
    pattern: str
    prefix_length: int


@dataclass(frozen=True)
class JwtFormat:
    issuer: str
    role_claim: str
    roles: frozenset[str]
    required_claims: tuple[str, ...]
    algorithm: str
    clock_skew_seconds: int


@dataclass(frozen=True)
class TargetProfile:
    name: str
    liveness_path: str
    readiness_path: str
    account_path: str
    auth_scheme: str
    lab_writes_allowed: bool
    check_pack: CheckPack
    health_style: HealthStyle
    api_key: ApiKeyFormat | None = None
    jwt: JwtFormat | None = None
    allowed_methods: frozenset[str] = frozenset(
        {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}
    )

    @property
    def credential_name(self) -> str:
        return "bearer token" if self.jwt is not None else "API key"

    @property
    def check_packs(self) -> frozenset[str]:
        return frozenset({GENERIC_PACK, self.check_pack})


BILLING = TargetProfile(
    name="billing",
    liveness_path="/health",
    readiness_path="/health/ready",
    account_path="/v1/account",
    auth_scheme="Bearer",
    lab_writes_allowed=True,
    check_pack="billing",
    health_style="billing",
    api_key=ApiKeyFormat(prefix="bk_", pattern=r"bk_[A-Za-z0-9_]{17,125}", prefix_length=12),
)

ORDERFLOW = TargetProfile(
    name="orderflow",
    liveness_path="/actuator/health/liveness",
    readiness_path="/actuator/health",
    account_path="/api/v1/users/me",
    auth_scheme="Bearer",
    lab_writes_allowed=False,
    check_pack="orderflow",
    health_style="actuator",
    jwt=JwtFormat(
        issuer="orderflow",
        role_claim="role",
        roles=frozenset({"ADMIN", "CUSTOMER"}),
        required_claims=("iss", "sub", "iat", "exp", "role"),
        algorithm="HS256",
        clock_skew_seconds=60,
    ),
    allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
)

TARGETS = {profile.name: profile for profile in (BILLING, ORDERFLOW)}


def get_target(name: str) -> TargetProfile:
    profile = TARGETS.get(name)
    if profile is None:
        raise ConfigError(
            f"Unknown target '{name}'.",
            hint=f"SUPPORTOPS_TARGET must be one of: {', '.join(TARGETS)}.",
        )
    return profile
