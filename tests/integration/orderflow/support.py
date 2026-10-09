from dataclasses import dataclass
from pathlib import Path
from typing import Any, LiteralString

import psycopg

ROOT = Path(__file__).resolve().parents[3]
SCHEMA_FILE = Path(__file__).with_name("schema.sql")
ROLE_SQL_DIR = ROOT / "docs" / "integrations"
ROLE_SQL_FILE = "orderflow-readonly-role.sql"
TEMPLATE_DATABASE = "orderflow_it"
ADMIN = "orderflow_admin"
READ_ONLY_ROLE = "supportops_orderflow_ro"
READABLE_TABLES = frozenset(
    {"products", "inventory_items", "inventory_movements", "orders", "order_items"}
)

SEED: LiteralString = """
INSERT INTO users (email, password_hash, role, created_at, updated_at) VALUES
    ('customer@example.test', 'not-a-real-hash', 'CUSTOMER', now(), now()),
    ('admin@example.test', 'not-a-real-hash', 'ADMIN', now(), now());

INSERT INTO products (sku, name, price, created_at, updated_at) VALUES
    ('WIDGET-1', 'Widget', 10.00, now(), now()),
    ('GADGET-2', 'Gadget', 25.50, now(), now());

INSERT INTO inventory_items (product_id, quantity_on_hand, updated_at) VALUES
    (1, 95, now()),
    (2, 48, now());

INSERT INTO orders (customer_id, status, total_amount, created_at, updated_at) VALUES
    (1, 'PENDING', 81.00, now(), now()),
    (1, 'CANCELLED', 20.00, now(), now()),
    (1, 'CONFIRMED', 20.00, now(), now());

INSERT INTO order_items
    (order_id, product_id, product_sku, product_name, unit_price, quantity, line_total) VALUES
    (1, 1, 'WIDGET-1', 'Widget', 10.00, 3, 30.00),
    (1, 2, 'GADGET-2', 'Gadget', 25.50, 2, 51.00),
    (2, 1, 'WIDGET-1', 'Widget', 10.00, 2, 20.00),
    (3, 1, 'WIDGET-1', 'Widget', 10.00, 2, 20.00);

INSERT INTO inventory_movements
    (product_id, quantity_change, reason, order_id, performed_by_user_id, note, created_at) VALUES
    (1, 100, 'RESTOCK', NULL, 2, 'Initial stock', now()),
    (2, 50, 'RESTOCK', NULL, 2, 'Initial stock', now()),
    (1, -3, 'ORDER_PLACED', 1, 1, NULL, now()),
    (2, -2, 'ORDER_PLACED', 1, 1, NULL, now()),
    (1, -2, 'ORDER_PLACED', 2, 1, NULL, now()),
    (1, 2, 'ORDER_CANCELLED', 2, 1, NULL, now()),
    (1, -2, 'ORDER_PLACED', 3, 1, NULL, now());
"""

DEFECTS: dict[str, LiteralString] = {
    "orderflow.inventory_mismatch": (
        "UPDATE inventory_items SET quantity_on_hand = 90 WHERE product_id = 1"
    ),
    "orderflow.order_total_mismatch": "UPDATE orders SET total_amount = 99.00 WHERE id = 1",
    "orderflow.orders_without_items": (
        "INSERT INTO orders (customer_id, status, total_amount, created_at, updated_at) "
        "VALUES (1, 'PENDING', 0, now(), now())"
    ),
}

PRIVILEGES: LiteralString = """
SELECT class.relname AS table_name,
       has_table_privilege(%(role)s, class.oid, 'SELECT') AS can_select,
       has_table_privilege(%(role)s, class.oid, 'INSERT, UPDATE, DELETE, TRUNCATE') AS can_write
FROM pg_class AS class
JOIN pg_namespace AS namespace ON namespace.oid = class.relnamespace
WHERE namespace.nspname = 'public' AND class.relkind = 'r'
ORDER BY class.relname
"""


@dataclass(frozen=True)
class OrderflowDatabase:
    host: str
    port: int
    database: str
    admin_password: str
    role_password: str

    def url(self, role: str = READ_ONLY_ROLE, *, password: str | None = None) -> str:
        secret = password if password is not None else self._password(role)
        return f"postgresql://{role}:{secret}@{self.host}:{self.port}/{self.database}"

    def _password(self, role: str) -> str:
        return self.admin_password if role == ADMIN else self.role_password


def admin_execute(
    database: OrderflowDatabase, statement: LiteralString, params: dict[str, Any] | None = None
) -> list[tuple[Any, ...]]:
    with psycopg.connect(database.url(ADMIN), autocommit=True) as connection:
        cursor = connection.execute(statement, params)
        return cursor.fetchall() if cursor.description else []
