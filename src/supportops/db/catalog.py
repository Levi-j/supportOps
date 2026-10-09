import re
from dataclasses import dataclass
from typing import Literal, LiteralString

from supportops.errors import ConfigError

CheckKind = Literal["connectivity", "consistency", "activity", "inventory", "lookup"]
ParameterValue = int | str | None

PLACEHOLDER = re.compile(r"%\((\w+)\)s")


@dataclass(frozen=True)
class Parameter:
    name: str
    description: str
    kind: Literal["integer", "text"] = "text"
    default: int | str | None = None
    required: bool = False
    pattern: str | None = None
    minimum: int | None = None
    maximum: int | None = None
    example: str = ""

    def parse(self, raw: str) -> int | str:
        value = raw.strip()
        if self.kind == "integer":
            if not re.fullmatch(r"\d+", value):
                raise ConfigError(f"--param {self.name} must be a whole number.")
            number = int(value)
            if (self.minimum is not None and number < self.minimum) or (
                self.maximum is not None and number > self.maximum
            ):
                raise ConfigError(
                    f"--param {self.name} must be between {self.minimum} and {self.maximum}."
                )
            return number
        if self.pattern is not None and not re.fullmatch(self.pattern, value):
            raise ConfigError(
                f"--param {self.name} has an invalid value.",
                hint=f"Expected {self.description.lower()}, for example "
                f"{self.name}={self.example}.",
            )
        return value


@dataclass(frozen=True)
class Check:
    name: str
    pack: Literal["generic", "billing"]
    kind: CheckKind
    description: str
    sql: LiteralString
    parameters: tuple[Parameter, ...] = ()
    one_of: tuple[str, ...] = ()
    ok_message: str = ""
    problem_message: str = ""

    def parameter(self, name: str) -> Parameter | None:
        return next((item for item in self.parameters if item.name == name), None)

    def usage(self) -> str:
        names = (
            list(self.one_of)
            if self.one_of
            else [item.name for item in self.parameters if item.required and item.default is None]
        )
        examples = [
            f"{name}={parameter.example}"
            for name in names
            if (parameter := self.parameter(name)) is not None
        ]
        joiner = " or " if self.one_of else " and "
        return (
            "Needs "
            + joiner.join(f"--param {name}" for name in names)
            + f" (for example {' or '.join(examples)})."
        )


_CONNECTIVITY_SQL: LiteralString = """
SELECT current_user AS role,
       current_database() AS database,
       current_setting('server_version') AS server_version,
       current_setting('transaction_read_only') AS transaction_read_only,
       current_setting('statement_timeout') AS statement_timeout,
       role.rolsuper AS superuser,
       role.rolcreaterole AS can_create_roles,
       role.rolcreatedb AS can_create_databases,
       role.rolbypassrls AS bypasses_row_security,
       role.rolreplication AS replication,
       role.rolsuper OR pg_has_role(current_user, 'pg_monitor', 'USAGE') AS can_monitor,
       ARRAY(
           SELECT namespace.nspname || '.' || class.relname
           FROM pg_class AS class
           JOIN pg_namespace AS namespace ON namespace.oid = class.relnamespace
           WHERE class.relkind IN ('r', 'p')
             AND namespace.nspname NOT IN ('pg_catalog', 'information_schema')
             AND NOT starts_with(namespace.nspname, 'pg_toast')
             AND has_table_privilege(class.oid, 'INSERT, UPDATE, DELETE, TRUNCATE')
           ORDER BY 1
       ) AS writable_tables
FROM pg_roles AS role
WHERE role.rolname = current_user
"""

_CONNECTIONS_SQL: LiteralString = """
SELECT coalesce(activity.usename, '(not visible)') AS role,
       coalesce(nullif(activity.application_name, ''), '(none)') AS application,
       coalesce(activity.state, '(not visible)') AS state,
       count(*) AS sessions,
       max(round(extract(epoch FROM now() - activity.state_change)))::bigint
           AS longest_in_state_seconds
FROM pg_stat_activity AS activity
WHERE activity.datname = current_database()
  AND activity.backend_type = 'client backend'
  AND activity.pid <> pg_backend_pid()
GROUP BY 1, 2, 3
ORDER BY sessions DESC, role, application, state
"""

_LONG_TRANSACTIONS_SQL: LiteralString = """
SELECT activity.pid,
       activity.usename AS role,
       coalesce(nullif(activity.application_name, ''), '(none)') AS application,
       activity.state,
       round(extract(epoch FROM now() - activity.xact_start))::bigint AS transaction_seconds,
       round(extract(epoch FROM now() - activity.state_change))::bigint AS in_state_seconds,
       activity.wait_event_type,
       (SELECT count(*) FROM pg_locks AS held WHERE held.pid = activity.pid AND held.granted)
           AS locks_held,
       left(regexp_replace(activity.query, '\\s+', ' ', 'g'), 120) AS last_query
FROM pg_stat_activity AS activity
WHERE activity.datname = current_database()
  AND activity.xact_start IS NOT NULL
  AND activity.pid <> pg_backend_pid()
  AND extract(epoch FROM now() - activity.xact_start) >= %(min_seconds)s
ORDER BY activity.xact_start
"""

_BLOCKING_SQL: LiteralString = """
SELECT blocked.pid AS blocked_pid,
       blocked.usename AS blocked_role,
       coalesce(nullif(blocked.application_name, ''), '(none)') AS blocked_application,
       round(extract(epoch FROM now() - blocked.query_start))::bigint AS waiting_seconds,
       blocked.wait_event_type || ':' || blocked.wait_event AS waiting_for,
       left(regexp_replace(blocked.query, '\\s+', ' ', 'g'), 120) AS blocked_query,
       blocker.pid AS blocking_pid,
       blocker.usename AS blocking_role,
       coalesce(nullif(blocker.application_name, ''), '(none)') AS blocking_application,
       blocker.state AS blocking_state,
       round(extract(epoch FROM now() - blocker.xact_start))::bigint
           AS blocking_transaction_seconds,
       left(regexp_replace(blocker.query, '\\s+', ' ', 'g'), 120) AS blocking_last_query
FROM pg_stat_activity AS blocked
CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS blocking (pid)
JOIN pg_stat_activity AS blocker ON blocker.pid = blocking.pid
WHERE blocked.datname = current_database()
ORDER BY waiting_seconds DESC, blocked.pid
"""

_TOTAL_MISMATCH_SQL: LiteralString = """
SELECT invoice.id AS invoice_id,
       invoice.number,
       invoice.account_id,
       invoice.status,
       invoice.total_cents AS stored_total_cents,
       coalesce(sum(line.amount_cents), 0)::bigint AS line_total_cents,
       count(line.id) AS line_count
FROM billing.invoices AS invoice
LEFT JOIN billing.invoice_lines AS line ON line.invoice_id = invoice.id
GROUP BY invoice.id
HAVING invoice.total_cents <> coalesce(sum(line.amount_cents), 0)
ORDER BY invoice.id
"""

_PAID_WITHOUT_PAYMENT_SQL: LiteralString = """
SELECT invoice.id AS invoice_id,
       invoice.number,
       invoice.account_id,
       invoice.total_cents,
       invoice.paid_at
FROM billing.invoices AS invoice
WHERE invoice.status = 'paid'
  AND NOT EXISTS (
      SELECT 1
      FROM billing.payments AS payment
      WHERE payment.invoice_id = invoice.id AND payment.status = 'succeeded'
  )
ORDER BY invoice.id
"""

_PAYMENT_ON_UNPAID_SQL: LiteralString = """
SELECT invoice.id AS invoice_id,
       invoice.number,
       invoice.account_id,
       invoice.status,
       invoice.total_cents,
       count(payment.id) AS succeeded_payments,
       sum(payment.amount_cents)::bigint AS paid_cents,
       max(payment.created_at) AS last_payment_at
FROM billing.invoices AS invoice
JOIN billing.payments AS payment
  ON payment.invoice_id = invoice.id AND payment.status = 'succeeded'
WHERE invoice.status <> 'paid'
GROUP BY invoice.id
ORDER BY invoice.id
"""

_DUPLICATE_PAYMENTS_SQL: LiteralString = """
SELECT invoice.id AS invoice_id,
       invoice.number,
       invoice.account_id,
       invoice.status,
       invoice.total_cents,
       count(payment.id) AS succeeded_payments,
       sum(payment.amount_cents)::bigint AS paid_cents,
       array_agg(payment.id ORDER BY payment.created_at) AS payment_ids,
       min(payment.created_at) AS first_payment_at,
       max(payment.created_at) AS last_payment_at
FROM billing.invoices AS invoice
JOIN billing.payments AS payment
  ON payment.invoice_id = invoice.id AND payment.status = 'succeeded'
GROUP BY invoice.id
HAVING count(payment.id) > 1
ORDER BY invoice.id
"""

_API_KEY_STATUS_SQL: LiteralString = """
SELECT api_key.key_prefix,
       api_key.id AS key_id,
       api_key.label,
       api_key.account_id,
       account.name AS account_name,
       account.status AS account_status,
       api_key.created_at,
       api_key.expires_at,
       api_key.revoked_at,
       CASE
           WHEN api_key.revoked_at IS NOT NULL AND api_key.revoked_at <= now() THEN 'revoked'
           WHEN api_key.expires_at IS NOT NULL AND api_key.expires_at <= now() THEN 'expired'
           ELSE 'active'
       END AS key_status
FROM billing.api_keys AS api_key
JOIN billing.accounts AS account ON account.id = api_key.account_id
WHERE api_key.key_prefix = %(prefix)s
"""

_INVOICE_LOOKUP_SQL: LiteralString = """
SELECT invoice.id AS invoice_id,
       invoice.number,
       invoice.account_id,
       account.name AS account_name,
       invoice.customer_id,
       invoice.status,
       invoice.currency,
       invoice.total_cents,
       (SELECT coalesce(sum(line.amount_cents), 0)::bigint
        FROM billing.invoice_lines AS line
        WHERE line.invoice_id = invoice.id) AS line_total_cents,
       (SELECT count(*)
        FROM billing.payments AS payment
        WHERE payment.invoice_id = invoice.id AND payment.status = 'succeeded')
           AS succeeded_payments,
       (SELECT count(*)
        FROM billing.payments AS payment
        WHERE payment.invoice_id = invoice.id AND payment.status = 'failed') AS failed_payments,
       invoice.due_date,
       invoice.created_at,
       invoice.paid_at
FROM billing.invoices AS invoice
JOIN billing.accounts AS account ON account.id = invoice.account_id
WHERE invoice.id = %(id)s OR invoice.number = %(number)s
ORDER BY invoice.account_id, invoice.id
"""

CATALOG: tuple[Check, ...] = (
    Check(
        name="db.connectivity",
        pack="generic",
        kind="connectivity",
        description="Server version, current role, read-only state and privilege warnings",
        sql=_CONNECTIVITY_SQL,
    ),
    Check(
        name="pg.connections",
        pack="generic",
        kind="inventory",
        description="Other sessions on this database by role, application and state",
        sql=_CONNECTIONS_SQL,
    ),
    Check(
        name="pg.long_transactions",
        pack="generic",
        kind="activity",
        description="Transactions open longer than min_seconds, including idle in transaction",
        sql=_LONG_TRANSACTIONS_SQL,
        parameters=(
            Parameter(
                name="min_seconds",
                description="Minimum transaction age in seconds",
                kind="integer",
                default=60,
                minimum=0,
                maximum=86_400,
                example="60",
            ),
        ),
        ok_message="No transaction has been open longer than {min_seconds} seconds.",
        problem_message="Transactions open longer than {min_seconds} seconds: {count}.",
    ),
    Check(
        name="pg.blocking_sessions",
        pack="generic",
        kind="activity",
        description="Sessions waiting for a lock, and the sessions blocking them",
        sql=_BLOCKING_SQL,
        ok_message="No session is waiting for a lock held by another session.",
        problem_message="Sessions waiting for a lock held by another session: {count}.",
    ),
    Check(
        name="billing.invoice_total_mismatch",
        pack="billing",
        kind="consistency",
        description="Invoices whose stored total differs from the sum of their lines",
        sql=_TOTAL_MISMATCH_SQL,
        ok_message="Every invoice total matches the sum of its lines.",
        problem_message="Invoices whose total differs from the sum of their lines: {count}.",
    ),
    Check(
        name="billing.paid_invoice_without_payment",
        pack="billing",
        kind="consistency",
        description="Invoices marked paid without a successful payment",
        sql=_PAID_WITHOUT_PAYMENT_SQL,
        ok_message="Every paid invoice has a successful payment.",
        problem_message="Paid invoices without a successful payment: {count}.",
    ),
    Check(
        name="billing.payment_on_unpaid_invoice",
        pack="billing",
        kind="consistency",
        description="Successful payments on invoices that are not marked paid",
        sql=_PAYMENT_ON_UNPAID_SQL,
        ok_message="No unpaid invoice has a successful payment.",
        problem_message="Unpaid invoices with a successful payment: {count}.",
    ),
    Check(
        name="billing.duplicate_payments",
        pack="billing",
        kind="consistency",
        description="Invoices with more than one successful payment",
        sql=_DUPLICATE_PAYMENTS_SQL,
        ok_message="No invoice has more than one successful payment.",
        problem_message="Invoices with more than one successful payment: {count}.",
    ),
    Check(
        name="billing.api_key_status",
        pack="billing",
        kind="lookup",
        description="Account, dates and lifecycle state of the API key with this prefix",
        sql=_API_KEY_STATUS_SQL,
        parameters=(
            Parameter(
                name="prefix",
                description="The 12-character key prefix",
                required=True,
                pattern=r"[A-Za-z0-9_]{12}",
                example="bk_juniper01",
            ),
        ),
        ok_message="Found the API key with prefix {prefix}.",
        problem_message="No API key has the prefix {prefix}.",
    ),
    Check(
        name="billing.invoice_lookup",
        pack="billing",
        kind="lookup",
        description="Status, totals and payment counts of an invoice, by ID or number",
        sql=_INVOICE_LOOKUP_SQL,
        parameters=(
            Parameter(
                name="id",
                description="An invoice ID",
                pattern=r"[A-Za-z0-9_-]{1,64}",
                example="inv_juniper_1003",
            ),
            Parameter(
                name="number",
                description="An invoice number",
                pattern=r"[A-Za-z0-9_-]{1,32}",
                example="INV-1003",
            ),
        ),
        one_of=("id", "number"),
        ok_message="Matching invoices: {count}.",
        problem_message="No invoice matches {lookup}.",
    ),
)

CHECKS: dict[str, Check] = {check.name: check for check in CATALOG}


def get_check(name: str) -> Check:
    check = CHECKS.get(name)
    if check is None:
        raise ConfigError(
            f"Unknown check '{name}'.",
            hint="List the available checks with 'supportops db checks'.",
        )
    return check


def placeholders(sql: str) -> set[str]:
    return set(PLACEHOLDER.findall(sql))
