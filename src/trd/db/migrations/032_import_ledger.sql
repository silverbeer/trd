-- What a broker export has already written into an account (SB-1418: the
-- Fidelity activity CSV), so importing the same file twice — or next month's
-- file, which overlaps this one — writes nothing twice.
--
-- A key per source row rather than a column on txn / income / cash_flow: one
-- export row becomes a write in any of the three (or in none — a core money
-- market reinvestment is cash staying cash), and ALTER on tables other tables
-- hold foreign keys to is the migration that has already failed once (see 029).
--
-- row_key is readable on purpose: run date | action class | symbol | quantity |
-- amount | occurrence. The occurrence is the row's index among IDENTICAL keys
-- in the same file, so two genuinely identical rows on one day both import,
-- and import once each.
CREATE TABLE IF NOT EXISTS import_row (
    account_id BIGINT NOT NULL REFERENCES account (id),
    source TEXT NOT NULL,
    row_key TEXT NOT NULL,
    imported_at TIMESTAMP NOT NULL,
    PRIMARY KEY (account_id, source, row_key)
);

-- An account a broker export feeds, and the name the person knows it by
-- ("Managed IRA"). Here rather than on `account`: same reason as above.
CREATE TABLE IF NOT EXISTS import_account (
    account_id BIGINT PRIMARY KEY REFERENCES account (id),
    source TEXT NOT NULL,
    display_name TEXT,
    created_at TIMESTAMP NOT NULL
);
