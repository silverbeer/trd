-- Why does this plan exist? Goal/purpose metadata, shown in plan ls/status.
ALTER TABLE contribution_plan ADD COLUMN IF NOT EXISTS note TEXT;
