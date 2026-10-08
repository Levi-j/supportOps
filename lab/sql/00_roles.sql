\set ON_ERROR_STOP on
\getenv billing_app_password BILLING_APP_DB_PASSWORD
\getenv supportops_ro_password SUPPORTOPS_RO_DB_PASSWORD

CREATE ROLE billing_app LOGIN PASSWORD :'billing_app_password';
CREATE ROLE supportops_ro LOGIN PASSWORD :'supportops_ro_password';

ALTER ROLE supportops_ro SET default_transaction_read_only = on;
ALTER ROLE supportops_ro SET statement_timeout = '5s';
GRANT pg_monitor TO supportops_ro;

REVOKE ALL ON DATABASE :"DBNAME" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"DBNAME" TO billing_app, supportops_ro;
