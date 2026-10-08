from dataclasses import dataclass
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[2] / "lab" / "sql"
POSTGRES_IMAGE = "postgres:18"
DATABASE = "billing"
PASSWORDS = {
    "lab_admin": "it_admin_password",
    "billing_app": "it_app_password",
    "supportops_ro": "it_readonly_password",
}


@dataclass(frozen=True)
class LabDatabase:
    host: str
    port: int

    def url(self, role: str, *, password: str | None = None, database: str = DATABASE) -> str:
        secret = PASSWORDS[role] if password is None else password
        return f"postgresql://{role}:{secret}@{self.host}:{self.port}/{database}"
