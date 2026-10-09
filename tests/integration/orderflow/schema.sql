-- OrderFlow schema fixture for SupportOps integration tests.
--
-- Source: orderFlow-backend at commit e878ddf, src/main/resources/db/migration/V1 to V6,
-- concatenated verbatim in migration order. Flyway's own history table is intentionally
-- omitted; nothing else differs.
--
-- Schema drift: if OrderFlow adds or changes migrations, the orderflow.* checks may stop
-- matching its real schema (PostgreSQL then reports missing_object or permission_denied
-- errors). To update, copy the new migrations here verbatim, record the new commit above, and
-- rerun: uv run pytest -m integration tests/integration/test_orderflow_db.py

-- V1__create_products_table.sql
CREATE TABLE products (
    id          BIGINT GENERATED ALWAYS AS IDENTITY,
    sku         VARCHAR(64)   NOT NULL,
    name        VARCHAR(200)  NOT NULL,
    description VARCHAR(2000),
    price       NUMERIC(12, 2) NOT NULL,
    active      BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ   NOT NULL,
    updated_at  TIMESTAMPTZ   NOT NULL,

    CONSTRAINT pk_products PRIMARY KEY (id),
    CONSTRAINT uk_products_sku UNIQUE (sku),
    CONSTRAINT ck_products_sku_format CHECK (sku ~ '^[A-Z0-9-]+$'),
    CONSTRAINT ck_products_price_positive CHECK (price > 0)
);

-- V2__create_users_table.sql
CREATE TABLE users (
    id            BIGINT GENERATED ALWAYS AS IDENTITY,
    email         VARCHAR(254) NOT NULL,
    password_hash VARCHAR(100) NOT NULL,
    role          VARCHAR(20)  NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL,
    updated_at    TIMESTAMPTZ  NOT NULL,

    CONSTRAINT pk_users PRIMARY KEY (id),
    CONSTRAINT uk_users_email UNIQUE (email),
    CONSTRAINT ck_users_email_lowercase CHECK (email = lower(email)),
    CONSTRAINT ck_users_role CHECK (role IN ('CUSTOMER', 'ADMIN'))
);

-- V3__create_inventory_tables.sql
CREATE TABLE inventory_items (
    product_id       BIGINT      NOT NULL,
    quantity_on_hand INTEGER     NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL,

    CONSTRAINT pk_inventory_items PRIMARY KEY (product_id),
    CONSTRAINT fk_inventory_items_product FOREIGN KEY (product_id) REFERENCES products (id),
    CONSTRAINT ck_inventory_items_quantity_nonnegative CHECK (quantity_on_hand >= 0)
);

CREATE TABLE inventory_movements (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY,
    product_id           BIGINT       NOT NULL,
    quantity_change      INTEGER      NOT NULL,
    reason               VARCHAR(30)  NOT NULL,
    order_id             BIGINT,
    performed_by_user_id BIGINT       NOT NULL,
    note                 VARCHAR(500),
    created_at           TIMESTAMPTZ  NOT NULL,

    CONSTRAINT pk_inventory_movements PRIMARY KEY (id),
    CONSTRAINT fk_inventory_movements_product FOREIGN KEY (product_id) REFERENCES products (id),
    CONSTRAINT fk_inventory_movements_user FOREIGN KEY (performed_by_user_id) REFERENCES users (id),
    CONSTRAINT ck_inventory_movements_quantity_change_nonzero CHECK (quantity_change <> 0),
    CONSTRAINT ck_inventory_movements_reason
        CHECK (reason IN ('RESTOCK', 'ADJUSTMENT', 'ORDER_PLACED', 'ORDER_CANCELLED'))
);

CREATE INDEX ix_inventory_movements_product_created_at ON inventory_movements (product_id, created_at DESC);

-- V4__create_orders_tables.sql
CREATE TABLE orders (
    id           BIGINT GENERATED ALWAYS AS IDENTITY,
    customer_id  BIGINT         NOT NULL,
    status       VARCHAR(20)    NOT NULL,
    total_amount NUMERIC(17, 2) NOT NULL,
    created_at   TIMESTAMPTZ    NOT NULL,
    updated_at   TIMESTAMPTZ    NOT NULL,

    CONSTRAINT pk_orders PRIMARY KEY (id),
    CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES users (id),
    CONSTRAINT ck_orders_status CHECK (status IN ('PENDING', 'CONFIRMED', 'CANCELLED')),
    CONSTRAINT ck_orders_total_amount CHECK (total_amount >= 0)
);

CREATE INDEX ix_orders_customer_created_at ON orders (customer_id, created_at DESC);

CREATE TABLE order_items (
    id           BIGINT GENERATED ALWAYS AS IDENTITY,
    order_id     BIGINT         NOT NULL,
    product_id   BIGINT         NOT NULL,
    product_sku  VARCHAR(64)    NOT NULL,
    product_name VARCHAR(200)   NOT NULL,
    unit_price   NUMERIC(12, 2) NOT NULL,
    quantity     INTEGER        NOT NULL,
    line_total   NUMERIC(15, 2) NOT NULL,

    CONSTRAINT pk_order_items PRIMARY KEY (id),
    CONSTRAINT fk_order_items_order FOREIGN KEY (order_id) REFERENCES orders (id),
    CONSTRAINT fk_order_items_product FOREIGN KEY (product_id) REFERENCES products (id),
    CONSTRAINT uk_order_items_order_product UNIQUE (order_id, product_id),
    CONSTRAINT ck_order_items_unit_price CHECK (unit_price > 0),
    CONSTRAINT ck_order_items_quantity CHECK (quantity BETWEEN 1 AND 1000),
    CONSTRAINT ck_order_items_line_total CHECK (line_total = unit_price * quantity)
);

ALTER TABLE inventory_movements
    ADD CONSTRAINT fk_inventory_movements_order FOREIGN KEY (order_id) REFERENCES orders (id);

-- V5__add_order_version.sql
ALTER TABLE orders
    ADD COLUMN version BIGINT NOT NULL DEFAULT 0;

CREATE INDEX ix_orders_status_created_at ON orders (status, created_at DESC);

-- V6__add_order_idempotency.sql
ALTER TABLE orders
    ADD COLUMN idempotency_key VARCHAR(100),
    ADD COLUMN request_hash    CHAR(64),
    ADD CONSTRAINT uk_orders_customer_idempotency_key UNIQUE (customer_id, idempotency_key);
