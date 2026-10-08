\set ON_ERROR_STOP on

CREATE SCHEMA billing;

CREATE TABLE billing.accounts (
    id         text        PRIMARY KEY,
    name       text        NOT NULL,
    status     text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_accounts_status CHECK (status IN ('active', 'suspended'))
);

CREATE TABLE billing.api_keys (
    id         text        PRIMARY KEY,
    account_id text        NOT NULL REFERENCES billing.accounts (id),
    key_prefix text        NOT NULL,
    key_hash   char(64)    NOT NULL,
    label      text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    revoked_at timestamptz,

    CONSTRAINT uk_api_keys_prefix UNIQUE (key_prefix),
    CONSTRAINT uk_api_keys_hash UNIQUE (key_hash)
);

CREATE TABLE billing.customers (
    id         text        PRIMARY KEY,
    account_id text        NOT NULL REFERENCES billing.accounts (id),
    name       text        NOT NULL,
    email      text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_customers_account ON billing.customers (account_id);

CREATE TABLE billing.invoices (
    id          text        PRIMARY KEY,
    account_id  text        NOT NULL REFERENCES billing.accounts (id),
    customer_id text        NOT NULL REFERENCES billing.customers (id),
    number      text        NOT NULL,
    status      text        NOT NULL,
    currency    char(3)     NOT NULL,
    total_cents bigint      NOT NULL DEFAULT 0,
    due_date    date        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    paid_at     timestamptz,

    CONSTRAINT uk_invoices_account_number UNIQUE (account_id, number),
    CONSTRAINT ck_invoices_status CHECK (status IN ('draft', 'open', 'paid', 'void')),
    CONSTRAINT ck_invoices_total CHECK (total_cents >= 0),
    CONSTRAINT ck_invoices_paid_at CHECK ((status = 'paid') = (paid_at IS NOT NULL))
);

CREATE INDEX ix_invoices_account_created ON billing.invoices (account_id, created_at DESC);
CREATE INDEX ix_invoices_customer ON billing.invoices (customer_id);

CREATE TABLE billing.invoice_lines (
    id                bigint  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invoice_id        text    NOT NULL REFERENCES billing.invoices (id),
    description       text    NOT NULL,
    quantity          integer NOT NULL,
    unit_amount_cents bigint  NOT NULL,
    amount_cents      bigint  NOT NULL,

    CONSTRAINT ck_invoice_lines_quantity CHECK (quantity > 0),
    CONSTRAINT ck_invoice_lines_unit_amount CHECK (unit_amount_cents >= 0),
    CONSTRAINT ck_invoice_lines_amount CHECK (amount_cents = quantity * unit_amount_cents)
);

CREATE INDEX ix_invoice_lines_invoice ON billing.invoice_lines (invoice_id);

CREATE TABLE billing.payments (
    id           text        PRIMARY KEY,
    invoice_id   text        NOT NULL REFERENCES billing.invoices (id),
    account_id   text        NOT NULL REFERENCES billing.accounts (id),
    amount_cents bigint      NOT NULL,
    status       text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_payments_amount CHECK (amount_cents > 0),
    CONSTRAINT ck_payments_status CHECK (status IN ('succeeded', 'failed'))
);

CREATE INDEX ix_payments_invoice ON billing.payments (invoice_id);

GRANT USAGE ON SCHEMA billing TO billing_app, supportops_ro;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA billing TO billing_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA billing TO billing_app;
GRANT SELECT ON ALL TABLES IN SCHEMA billing TO supportops_ro;
