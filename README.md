# Bitbucket → GitLab Backup & Restore

Back up an entire Bitbucket Cloud workspace — projects, repositories, pull requests, and comments — to a local snapshot, then restore it to a GitLab instance.

---

## Overview

```
Bitbucket Cloud                   Local snapshot                  GitLab
─────────────────                 ──────────────                  ──────
Workspace          ──backup.py──► groups/                ──restore.py──► Group
  Project                           workspace/                           └─ Subgroup
    Repository                        project-name/                           └─ Project
      Pull Requests                     repo-slug/                                 Pull Requests → MRs
      Comments                            repo.git            (git push --mirror)   Comments → Notes
```

### Bitbucket → GitLab concept mapping

| Bitbucket | GitLab |
|---|---|
| Workspace | Top-level Group |
| Project | Subgroup |
| Repository | Project |
| Pull Request | Merge Request |
| PR Comment | Note (with original author attribution) |

---

## Prerequisites

- **Python 3.10+**
- **Git** (CLI, must be on `PATH`)
- **Bitbucket App Password** with scopes: `Account (read)`, `Workspace membership (read)`, `Projects (read)`, `Repositories (read)`, `Pull requests (read)`, `Pipelines (read)`
  - Create at: Bitbucket → Personal settings → App passwords
- **GitLab personal access token** with scopes: `api`
  - Create at: GitLab → Preferences → Access tokens

---

## Setup

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure credentials
cp .env.example .env             # then edit .env with your values
```

> **Security:** `.env` is listed in `.gitignore` and will never be committed. Do not remove it from `.gitignore`.

---

## Configuration

All settings live in `.env`. CLI flags (where available) override `.env` values.

Copy `.env.example` to `.env` and fill in the values:

| Variable | Required | Description |
|---|---|---|
| `BITBUCKET_WORKSPACE` | Yes | Workspace slug (visible in your Bitbucket URL) |
| `BITBUCKET_USERNAME` | Yes | Your Bitbucket username (not email) |
| `BITBUCKET_APP_PASSWORD` | Yes | App password with the scopes listed above |
| `BACKUP_DIR` | No | Local directory for snapshots (default: `./backups`) |
| `BACKUP_WORKERS` | No | Parallel repo workers (default: `4`) |
| `RESTORE_GITLAB_URL` | Yes | GitLab instance URL (e.g. `https://gitlab.com`) |
| `RESTORE_GITLAB_TOKEN` | Yes | GitLab personal access token with `api` scope |
| `RESTORE_TARGET_GROUP` | Yes | GitLab group path where the workspace will be restored |

`.env.example`:
```dotenv
BITBUCKET_WORKSPACE=my-workspace
BITBUCKET_USERNAME=my-username
BITBUCKET_APP_PASSWORD=ATBB-xxxxxxxxxxxxxxxxxxxx

BACKUP_DIR=./backups
BACKUP_WORKERS=4

RESTORE_GITLAB_URL=https://gitlab.com
RESTORE_GITLAB_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
RESTORE_TARGET_GROUP=my-restored-group
```

---

## Backup

### What gets backed up

| Data | Method |
|---|---|
| Git repository (all branches, tags, history) | `git clone --mirror` |
| Pull requests (open, merged, declined) | Bitbucket REST API → JSON |
| PR comments | Bitbucket REST API → embedded in PR JSON |
| Project (subgroup) metadata | Bitbucket REST API → JSON |
| Workspace metadata and members | Bitbucket REST API → JSON |

> Subsequent runs on the same `BACKUP_DIR` are **incremental**: existing bare clones are updated with `git remote update --prune` instead of re-cloned.

### Filtering with `repos_filter.txt`

By default, all repositories in the workspace are backed up. To limit the scope, edit `repos_filter.txt`:

```
# Back up all repos in a Bitbucket project (use the project KEY, not name)
project:DEVOPS
project:PLATFORM

# Back up individual repos by slug
standalone-repo
another-specific-repo
```

- Lines starting with `#` are comments and are ignored.
- Leave the file empty (or with only comments) to back up the entire workspace.
- You can mix `project:` entries and individual repo slugs freely.
- If an entry is not found in the workspace, a warning is printed and the script continues.

### Running the backup

```bash
python backup.py [OPTIONS]

Options:
  --workspace WORKSPACE   Override BITBUCKET_WORKSPACE from .env
  --output-dir DIR        Override BACKUP_DIR from .env
  --dry-run               Discover and log without writing any files
```

Examples:

```bash
# Full backup using .env settings
python backup.py

# Test run — see what would be backed up without touching the filesystem
python backup.py --dry-run

# Back up a different workspace to a custom directory
python backup.py --workspace other-workspace --output-dir /mnt/backup/bitbucket
```

### Output directory layout

Each run creates a timestamped snapshot directory:

```
backups/
└── 2026-03-10T14-32-00/
    ├── backup_log.json           ← machine-readable log
    ├── backup_summary.txt        ← human-readable report
    └── groups/
        └── my-workspace/
            ├── group_meta.json   ← workspace metadata
            ├── members.json
            └── devops/           ← Bitbucket project (sanitized name)
                ├── group_meta.json
                └── my-repo/      ← repository
                    ├── project_meta.json
                    ├── repo.git/              ← bare mirror clone
                    └── merge_requests.json    ← PRs with embedded comments
```

### Log files

**`backup_log.json`** — machine-readable:

```json
{
  "started_at": "2026-03-10T14:32:00+00:00",
  "finished_at": "2026-03-10T15:01:45+00:00",
  "duration_seconds": 1785,
  "groups": ["my-workspace", "my-workspace/devops"],
  "projects": [
    { "path": "my-workspace/devops/my-repo", "status": "ok", "duration_seconds": 42, "errors": [] }
  ],
  "totals": { "groups": 2, "projects": 15, "merge_requests": 312, "errors": 0 }
}
```

**`backup_summary.txt`** — human-readable:

```
Bitbucket Backup Summary
============================================================
Started:   2026-03-10T14:32:00+00:00
Finished:  2026-03-10T15:01:45+00:00
Duration:  1785s (29.8 min)

Groups backed up:   2
Projects backed up: 15
Pull requests:      312
Errors:             0

────────────────────────────────────────────────────────────
Repositories:
  [OK     ] my-workspace/devops/my-repo (42s)
  [PARTIAL ] my-workspace/devops/other-repo (18s)
             ! PR 47 comments: 403 Forbidden
```

Exit codes: `0` = all OK, `1` = one or more repositories had `status: error`.

---

## Restore

Restores a local snapshot to a GitLab instance. Only **project hierarchy, git repositories, and pull requests with comments** are restored.

### What gets restored

| Snapshot data | GitLab result |
|---|---|
| Workspace directory | Top-level GitLab group |
| Project directories | Subgroups |
| `repo.git/` | `git push --mirror` to new GitLab project |
| `merge_requests.json` | GitLab Merge Requests |
| PR comments (`_notes`) | MR Notes (prefixed with original Bitbucket author) |

**Pull request state mapping:**

| Bitbucket state | GitLab state |
|---|---|
| `OPEN` | `opened` |
| `MERGED` | `closed` (with a note: *"this PR was merged in Bitbucket"*) |
| `DECLINED` / `SUPERSEDED` | `closed` |

**When source or target branch is missing** (branch was deleted after merge), the PR is created as a GitLab Issue titled `[PR] <original title>` so the content is not lost.

**Author attribution:** GitLab does not allow impersonating users via API. All restored MR descriptions and comments are prefixed with:
> *[Restored from Bitbucket — original author: Display Name on YYYY-MM-DD]*

### Running the restore

```bash
python restore.py --backup-dir PATH [OPTIONS]

Required:
  --backup-dir PATH       Path to a snapshot directory (e.g. backups/2026-03-10T14-32-00)

Options:
  --target-group GROUP    Override RESTORE_TARGET_GROUP from .env
  --force                 Re-apply metadata even if the group/project already exists
  --dry-run               Show what would be done without making any changes
```

Examples:

```bash
# Restore a specific snapshot
python restore.py --backup-dir backups/2026-03-10T14-32-00

# Dry run to preview what would be created
python restore.py --backup-dir backups/2026-03-10T14-32-00 --dry-run

# Restore to a different GitLab group than configured in .env
python restore.py --backup-dir backups/2026-03-10T14-32-00 --target-group my-other-group

# Re-run after a partial failure
python restore.py --backup-dir backups/2026-03-10T14-32-00 --force
```

Restore is **idempotent**: groups and projects that already exist on the target GitLab instance are skipped unless `--force` is passed.

---

## Known Limitations

| Limitation | Behaviour |
|---|---|
| Mercurial repositories (`scm: hg`) | Git clone is skipped; warning is logged; JSON metadata is still saved |
| Bitbucket pipeline variables marked as secured | Key name is saved; value is replaced with `*** SECURED — re-enter manually ***` |
| Webhook secrets | URL and event triggers are saved; secret token is replaced with `*** REDACTED ***` |
| Author impersonation on GitLab | Not possible via API; all restored content is prefixed with original author info |
| Bitbucket API rate limit (~1000 req/hr) | Handled with automatic exponential back-off; reduce `BACKUP_WORKERS` if hitting limits |
| PRs whose branches were deleted | Created as GitLab Issues with `[PR]` prefix so content is preserved |

---

## Security

- **`.env` is listed in `.gitignore`** and will not be committed. Keep it that way.
- App passwords and API tokens are used only for authentication; they appear in git clone URLs temporarily in memory but are never written to disk.
- Webhook secrets and secured pipeline variables in the backup JSON are redacted before saving.
