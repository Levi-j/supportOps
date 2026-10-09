-- Read-only PostgreSQL role for SupportOps diagnostics against OrderFlow.
--
-- Run ONCE, as the OrderFlow database owner or another administrator, while connected to the
-- OrderFlow database with psql, for example:
--
--   psql -d orderflow -f orderflow-readonly-role.sql
--
-- It creates one new role and grants it SELECT on the five tables the SupportOps checks read.
-- It changes no existing role, table or row, and grants nothing on users (emails and password
-- hashes). The file contains no password: set one interactively afterwards with
--
--   \password supportops_orderflow_ro
--
-- :"DBNAME" is psql's built-in variable for the database you are connected to, so the CONNECT
-- grant always targets that database.

\set ON_ERROR_STOP on

CREATE ROLE supportops_orderflow_ro LOGIN;
ALTER ROLE supportops_orderflow_ro SET default_transaction_read_only = on;
ALTER ROLE supportops_orderflow_ro SET statement_timeout = '5s';

GRANT CONNECT ON DATABASE :"DBNAME" TO supportops_orderflow_ro;
GRANT USAGE ON SCHEMA public TO supportops_orderflow_ro;
GRANT SELECT ON public.products,
                public.inventory_items,
                public.inventory_movements,
                public.orders,
                public.order_items
   TO supportops_orderflow_ro;

-- Optional: lets the generic pg.* session checks see other roles' sessions. Without it those
-- checks report their results as incomplete rather than clean.
-- GRANT pg_monitor TO supportops_orderflow_ro;
