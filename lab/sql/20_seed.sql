\set ON_ERROR_STOP on

INSERT INTO billing.accounts (id, name, status, created_at) VALUES
    ('acct_juniper', 'Juniper Dental Group', 'active', now() - interval '400 days'),
    ('acct_kestrel', 'Kestrel Logistics', 'active', now() - interval '250 days'),
    ('acct_alder', 'Alder & Finch Studio', 'suspended', now() - interval '180 days');

INSERT INTO billing.api_keys (id, account_id, key_prefix, key_hash, label, created_at, revoked_at)
SELECT id, account_id, left(lab_key, 12), encode(sha256(convert_to(lab_key, 'UTF8')), 'hex'),
       label, now() - created_ago, now() - revoked_ago
FROM (VALUES
    ('key_juniper_old', 'acct_juniper', 'bk_juniper00_lab_only_not_a_real_key',
     'Practice software (old)', interval '390 days', interval '90 days'),
    ('key_juniper_main', 'acct_juniper', 'bk_juniper01_lab_only_not_a_real_key',
     'Practice software', interval '91 days', NULL::interval),
    ('key_kestrel_main', 'acct_kestrel', 'bk_kestrel01_lab_only_not_a_real_key',
     'Dispatch integration', interval '240 days', NULL::interval),
    ('key_alder_main', 'acct_alder', 'bk_alderfin1_lab_only_not_a_real_key',
     'Studio website', interval '170 days', NULL::interval)
) AS seed (id, account_id, lab_key, label, created_ago, revoked_ago);

INSERT INTO billing.customers (id, account_id, name, email, created_at) VALUES
    ('cus_juniper_main', 'acct_juniper', 'Main Street Clinic',
     'ap@juniper-dental.example', now() - interval '390 days'),
    ('cus_juniper_harbor', 'acct_juniper', 'Harbor Clinic',
     'harbor@juniper-dental.example', now() - interval '200 days'),
    ('cus_kestrel_ops', 'acct_kestrel', 'Operations',
     'billing@kestrel-logistics.example', now() - interval '240 days'),
    ('cus_kestrel_fleet', 'acct_kestrel', 'Fleet',
     'fleet@kestrel-logistics.example', now() - interval '120 days'),
    ('cus_alder_studio', 'acct_alder', 'Alder & Finch Studio',
     'accounts@alder-finch.example', now() - interval '170 days');

INSERT INTO billing.invoices
    (id, account_id, customer_id, number, status, currency, due_date, created_at, updated_at, paid_at)
SELECT id, account_id, customer_id, number, status, currency,
       (now() - created_ago + interval '30 days')::date,
       now() - created_ago,
       now() - created_ago + CASE WHEN status = 'paid' THEN interval '5 days' ELSE interval '0' END,
       CASE WHEN status = 'paid' THEN now() - created_ago + interval '5 days' END
FROM (VALUES
    ('inv_juniper_1001', 'acct_juniper', 'cus_juniper_main', 'INV-1001', 'paid', 'EUR', interval '60 days'),
    ('inv_juniper_1002', 'acct_juniper', 'cus_juniper_harbor', 'INV-1002', 'paid', 'EUR', interval '30 days'),
    ('inv_juniper_1003', 'acct_juniper', 'cus_juniper_main', 'INV-1003', 'open', 'EUR', interval '10 days'),
    ('inv_juniper_1004', 'acct_juniper', 'cus_juniper_harbor', 'INV-1004', 'draft', 'EUR', interval '1 day'),
    ('inv_kestrel_2001', 'acct_kestrel', 'cus_kestrel_ops', 'INV-2001', 'paid', 'USD', interval '45 days'),
    ('inv_kestrel_2002', 'acct_kestrel', 'cus_kestrel_ops', 'INV-2002', 'open', 'USD', interval '12 days'),
    ('inv_kestrel_2003', 'acct_kestrel', 'cus_kestrel_fleet', 'INV-2003', 'open', 'USD', interval '5 days'),
    ('inv_kestrel_2004', 'acct_kestrel', 'cus_kestrel_fleet', 'INV-2004', 'void', 'USD', interval '40 days'),
    ('inv_alder_3001', 'acct_alder', 'cus_alder_studio', 'INV-3001', 'open', 'EUR', interval '75 days')
) AS seed (id, account_id, customer_id, number, status, currency, created_ago);

INSERT INTO billing.invoice_lines (invoice_id, description, quantity, unit_amount_cents, amount_cents)
SELECT invoice_id, description, quantity, unit_amount_cents, quantity * unit_amount_cents
FROM (VALUES
    ('inv_juniper_1001', 'Practice management plan (monthly)', 1, 4900),
    ('inv_juniper_1001', 'Additional staff seats', 3, 1500),
    ('inv_juniper_1002', 'Practice management plan (monthly)', 1, 4900),
    ('inv_juniper_1003', 'Practice management plan (monthly)', 1, 4900),
    ('inv_juniper_1003', 'SMS appointment reminders', 400, 25),
    ('inv_juniper_1004', 'Practice management plan (monthly)', 1, 4900),
    ('inv_kestrel_2001', 'Route optimisation (monthly)', 1, 29900),
    ('inv_kestrel_2001', 'Tracked vehicles', 12, 800),
    ('inv_kestrel_2002', 'Route optimisation (monthly)', 1, 29900),
    ('inv_kestrel_2002', 'Tracked vehicles', 14, 800),
    ('inv_kestrel_2003', 'Driver mobile licences', 20, 450),
    ('inv_kestrel_2004', 'Onboarding workshop', 1, 75000),
    ('inv_alder_3001', 'Website booking widget (annual)', 1, 18000)
) AS seed (invoice_id, description, quantity, unit_amount_cents);

UPDATE billing.invoices AS invoice
SET total_cents = lines.total
FROM (
    SELECT invoice_id, sum(amount_cents) AS total
    FROM billing.invoice_lines
    GROUP BY invoice_id
) AS lines
WHERE lines.invoice_id = invoice.id;

INSERT INTO billing.payments (id, invoice_id, account_id, amount_cents, status, created_at)
SELECT 'pay_' || substr(id, 5), id, account_id, total_cents, 'succeeded', paid_at
FROM billing.invoices
WHERE status = 'paid';

INSERT INTO billing.payments (id, invoice_id, account_id, amount_cents, status, created_at)
SELECT 'pay_juniper_1003_declined', id, account_id, total_cents, 'failed', now() - interval '2 days'
FROM billing.invoices
WHERE id = 'inv_juniper_1003';
