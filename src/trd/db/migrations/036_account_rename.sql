-- An account's new name (SB-1420).
--
-- `account.name` cannot be updated: DuckDB runs an UPDATE as delete + insert,
-- and refuses it on any row another table references by foreign key — which is
-- every account that holds anything. So a rename is a row here, and readers go
-- through `account_named`, which prefers it. The old name stops resolving.
CREATE TABLE IF NOT EXISTS account_rename (
    account_id BIGINT PRIMARY KEY REFERENCES account (id),
    name TEXT NOT NULL UNIQUE,
    renamed_at TIMESTAMP NOT NULL
);

CREATE OR REPLACE VIEW account_named AS
SELECT a.id, COALESCE(r.name, a.name) AS name, a.type, a.currency, a.created_at
FROM account a
LEFT JOIN account_rename r ON r.account_id = a.id;
