# Rolling back to v0.2.0

The state schema moved to `user_version=2` (a `groups` table plus record/event
payload fields). Rollback is a **restore**, not a downgrade of the Python package:
a v0.2.0 binary refuses to open a v2 database (`MIGRATION_REQUIRED` /
`SCHEMA_UNSUPPORTED`) instead of writing half-understood rows.

## Preconditions

Roll back only when **both** are true:

1. no v0.3.0 supervisor or control operation is running
   (`control.lock` / `execution.lock` are free; `zoe task list --json` shows no
   `RUNNING` run);
2. no new external effect was produced by v0.3.0 — no PR pushed, no remote review
   submitted, no merge. If there was one, reconcile it first (see below).

## Restore

```bash
# 1. stop everything and confirm nothing is running
zoe task list --json | jq '[.[] | select(.state == "RUNNING")] | length'   # expect 0

# 2. keep the v2 state for forensics, then restore the v1 backup
cp -a ~/.local/share/zoe ~/.local/share/zoe.v2-$(date +%s)
cp ~/.local/share/zoe/backups/state-v1-<ts>.db ~/.local/share/zoe/state.db
rm -f ~/.local/share/zoe/state.db-wal ~/.local/share/zoe/state.db-shm

# 3. reinstall the previous version and verify it opens the state
pip install 'zoe-runtime==0.2.0'
zoe --version && zoe doctor --json
```

`zoe state migrate --to 2` writes the backup to `home/backups/state-v1-<epoch>.db`
before it changes anything, so the restore source always exists.

## Remote effects must be reconciled, never reverted blindly

If v0.3.0 already pushed a branch, opened a PR or submitted a review:

1. reconcile the remote first (`zoe reconcile --project P --json`, then read the
   PR state in the host UI);
2. record what happened in the task's `acceptance` material — a downgrade does not
   erase a published commit;
3. only then restore state. Never `git reset`, force-push or delete a PR to make
   the local state look consistent with v0.2.0.

## Requests and idempotency after rollback

- Operation records restore with the database, so replaying a request id with the
  **same** content stays idempotent.
- A request that only exists in the v2 database (created after the migration) is
  gone after a restore: re-issue it with a **new** `request_id` and explain the
  rollback in the task material. Do not reuse an id whose outcome is unknown.
- `legacy_admin_declaration` records are stored per profile and survive a restore
  unchanged; they are still not real-e2e evidence in either direction.

## What cannot be rolled back

- Real remote history (commits, PRs, reviews).
- Files the agent changed in a worktree: restore the repository from git, not from
  the state database.
- Adapter capability: `VERIFIED` never travels with a downgrade. After restoring,
  every adapter is back to whatever its own profile evidence says.
