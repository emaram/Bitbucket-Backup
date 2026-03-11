#!/usr/bin/env python3
"""
Bitbucket → GitLab Restore Tool
    Eugen Marin, 2026-03-10 - ARRC DevOps

Restores a Bitbucket backup snapshot (created by backup.py) to a GitLab instance.
Only restores: project hierarchy, git repositories, and pull requests with comments.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import gitlab
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

load_dotenv()
console = Console()

# Bitbucket PR state → GitLab MR state
PR_STATE_MAP = {
    "OPEN":     "opened",
    "MERGED":   "merged",
    "DECLINED": "closed",
    "SUPERSEDED": "closed",
}


# ─── Configuration ────────────────────────────────────────────────────────────

def load_config(args):
    cfg = {
        "url":          os.getenv("RESTORE_GITLAB_URL", os.getenv("GITLAB_URL", "https://gitlab.com")),
        "token":        os.getenv("RESTORE_GITLAB_TOKEN", os.getenv("GITLAB_TOKEN", "")),
        "target_group": args.target_group or os.getenv("RESTORE_TARGET_GROUP", ""),
        "backup_dir":   Path(args.backup_dir),
        "force":        args.force,
        "dry_run":      args.dry_run,
    }
    if not cfg["token"]:
        console.print("[bold red]ERROR:[/] RESTORE_GITLAB_TOKEN (or GITLAB_TOKEN) is not set.")
        sys.exit(1)
    if not cfg["target_group"]:
        console.print("[bold red]ERROR:[/] RESTORE_TARGET_GROUP is not set.")
        sys.exit(1)
    if not cfg["backup_dir"].exists():
        console.print(f"[bold red]ERROR:[/] Backup directory does not exist: {cfg['backup_dir']}")
        sys.exit(1)
    return cfg


# ─── Helpers ──────────────────────────────────────────────────────────────────

def read_json(path: Path):
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def with_retry(fn, retries=5, backoff=10):
    """Call fn(), retrying on GitLab HTTP 429 with exponential back-off."""
    for attempt in range(retries):
        try:
            return fn()
        except gitlab.exceptions.GitlabHttpError as e:
            if e.response_code == 429 and attempt < retries - 1:
                wait = backoff * (2 ** attempt)
                console.print(f"  [yellow]Rate limited — waiting {wait}s...[/]")
                time.sleep(wait)
            else:
                raise


def sanitize_gitlab_path(name: str) -> str:
    """
    Sanitize a name for use as a GitLab group/project path.
    Must match backup.py sanitize_dirname() function.
    
    GitLab path rules:
    - Allowed: letters, digits, '_', '-', '.'
    - Cannot start with: '-', '_', '.'
    - Cannot end with: '-', '_', '.', '.git', '.atom'
    """
    sanitized = name
    
    # Replace Unicode dashes with regular hyphen
    sanitized = sanitized.replace('–', '-')  # U+2013 en-dash
    sanitized = sanitized.replace('—', '-')  # U+2014 em-dash
    sanitized = sanitized.replace('−', '-')  # U+2212 minus sign
    
    # Replace special characters
    sanitized = sanitized.replace('&', 'and')
    sanitized = sanitized.replace('+', 'plus')
    sanitized = sanitized.replace('@', 'at')
    
    # Replace invalid filesystem/GitLab characters with hyphens
    invalid_chars = '<>:"/\\|?*!#$%^()[]{}=~`'
    for char in invalid_chars:
        sanitized = sanitized.replace(char, '-')
    
    # Remove leading/trailing whitespace and dots
    sanitized = sanitized.strip('. ')
    
    # Replace multiple spaces with single hyphen
    sanitized = '-'.join(sanitized.split())
    
    # Replace multiple consecutive hyphens with single hyphen
    while '--' in sanitized:
        sanitized = sanitized.replace('--', '-')
    
    # Remove leading/trailing hyphens, underscores, dots
    sanitized = sanitized.strip('-_.')
    
    # Ensure doesn't end with .git or .atom
    for suffix in ['.git', '.atom']:
        if sanitized.lower().endswith(suffix):
            sanitized = sanitized[:-len(suffix)].rstrip('-_.')
    
    # Limit length to avoid filesystem issues
    if len(sanitized) > 100:
        sanitized = sanitized[:100].rstrip('-_.')
    
    # If empty after sanitization, use fallback
    if not sanitized:
        sanitized = 'unnamed'
    
    return sanitized.lower()


def remap_path(original_path: str, original_root: str, target_root: str) -> str:
    """Replace original_root prefix with target_root in a full namespace path."""
    if original_path.startswith(original_root + "/"):
        return target_root + original_path[len(original_root):]
    if original_path == original_root:
        return target_root
    return target_root + "/" + original_path


# ─── Group Tree Restore ───────────────────────────────────────────────────────

def restore_group_tree(gl, backup_dir: Path, target_root: str, dry_run: bool) -> dict:
    """
    Walk backup_dir/groups/, recreate the group/subgroup hierarchy on GitLab.
    Returns mapping: original_full_path -> restored GitLab group object.
    """
    groups_root = backup_dir / "groups"
    if not groups_root.exists():
        console.print("[red]No 'groups' directory found in backup.[/]")
        sys.exit(1)

    meta_files = sorted(groups_root.rglob("group_meta.json"), key=lambda p: len(p.parts))
    restored_groups = {}

    console.print(f"[bold]Restoring group tree ({len(meta_files)} groups)...[/]")

    for meta_file in meta_files:
        meta = read_json(meta_file)
        if meta is None:
            continue

        # Derive original path from directory structure
        rel = meta_file.parent.relative_to(groups_root)
        original_path = str(rel).replace(os.sep, "/")

        # Determine root from first entry
        original_root = list(groups_root.iterdir())[0].name

        new_path = remap_path(original_path, original_root, target_root)
        parts = new_path.split("/")
        
        # The directory name is already sanitized by backup.py
        # But we need to re-sanitize from the display name for proper GitLab paths
        display_name = meta.get("name") or meta.get("full_name") or meta.get("key") or parts[-1]
        new_slug = sanitize_gitlab_path(display_name)
        new_name = display_name
        
        parent_path = "/".join(parts[:-1]) if len(parts) > 1 else None

        if dry_run:
            console.print(f"  [dim][dry-run] Would create group: {new_path}[/]")
            restored_groups[original_path] = None
            continue

        parent_id = None
        if parent_path:
            try:
                parent_group = gl.groups.get(parent_path)
                parent_id = parent_group.id
            except gitlab.exceptions.GitlabGetError:
                console.print(f"  [red]Parent group not found: {parent_path}[/]")
                continue

        # Build the actual path with sanitized slug
        if parent_path:
            actual_new_path = f"{parent_path}/{new_slug}"
        else:
            actual_new_path = new_slug

        try:
            existing = gl.groups.get(actual_new_path)
            console.print(f"  [yellow]EXISTS[/]   {actual_new_path}")
            restored_groups[original_path] = existing
            continue
        except gitlab.exceptions.GitlabGetError:
            pass

        try:
            create_params = {
                "name": new_name,
                "path": new_slug,
                "description": meta.get("description") or "",
                "visibility": "private",  # Always private to avoid parent/child conflicts
            }
            if parent_id:
                create_params["parent_id"] = parent_id
            group = with_retry(lambda p=create_params: gl.groups.create(p))
            console.print(f"  [green]CREATED[/]  {actual_new_path}")
            restored_groups[original_path] = group
        except Exception as e:
            console.print(f"  [red]FAILED[/]   {actual_new_path}: {e}")

    return restored_groups


# ─── Git Push Mirror ──────────────────────────────────────────────────────────

def push_mirror(repo_git_dir: Path, remote_url_with_token: str, dry_run: bool) -> bool:
    if dry_run:
        console.print(f"    [dim][dry-run] Would push mirror from {repo_git_dir}[/]")
        return True
    result = subprocess.run(
        ["git", "push", "--mirror", remote_url_with_token],
        cwd=repo_git_dir, capture_output=True, text=True
    )
    if result.returncode != 0:
        console.print(f"    [red]git push error:[/] {result.stderr.strip()}")
        return False
    return True


# ─── Metadata Restore ─────────────────────────────────────────────────────────

def _note_prefix(author: dict, created_on: str = "") -> str:
    username = author.get("display_name") or author.get("nickname") or author.get("username") or "unknown"
    date_str = f" on {created_on[:10]}" if created_on else ""
    return f"*[Restored from Bitbucket — original author: {username}{date_str}]*\n\n"


def restore_merge_requests(project, prs: list, dry_run: bool):
    """Restore Bitbucket pull requests as GitLab merge requests."""
    if not prs:
        return
    try:
        branches = {b.name for b in project.branches.list(all=True)}
    except Exception:
        branches = set()

    existing_titles = {mr.title for mr in project.mergerequests.list(all=True)}

    for pr in prs:
        title = pr.get("title", "")
        if not title or title in existing_titles:
            continue

        source = (pr.get("source") or {}).get("branch", {}).get("name", "")
        target = (pr.get("destination") or {}).get("branch", {}).get("name", "main")
        bb_state = pr.get("state", "OPEN")
        gl_state = PR_STATE_MAP.get(bb_state, "opened")
        description = pr.get("description") or pr.get("content", {}).get("raw") or ""
        author = pr.get("author") or {}
        created_on = pr.get("created_on", "")

        if source not in branches or target not in branches or source == target:
            # Cannot create MR without both branches — store as an issue
            try:
                if not dry_run:
                    body = (
                        f"**[Restored Bitbucket PR #{pr.get('id')} — branches unavailable]**\n\n"
                        f"**Source:** `{source}`  **Target:** `{target}`\n"
                        f"**State:** {bb_state}\n"
                        f"**Original author:** {author.get('display_name', 'unknown')}\n\n"
                        f"{description}"
                    )
                    project.issues.create({"title": f"[PR] {title}", "description": body})
            except Exception:
                pass
            continue

        try:
            params = {
                "title": title,
                "source_branch": source,
                "target_branch": target,
                "description": (
                    _note_prefix(author, created_on) + description if description
                    else _note_prefix(author, created_on).rstrip()
                ),
            }
            if not dry_run:
                new_mr = project.mergerequests.create(params)
                for note in pr.get("_notes", []):
                    body = (
                        note.get("content", {}).get("raw")
                        or note.get("inline", {}).get("to", "")
                        or ""
                    )
                    if body:
                        try:
                            note_author = note.get("author") or note.get("user") or {}
                            prefix = _note_prefix(note_author, note.get("created_on", ""))
                            new_mr.notes.create({"body": prefix + body})
                        except Exception:
                            pass
                if gl_state == "merged":
                    new_mr.notes.create({"body": "_[Restored: this PR was merged in Bitbucket]_"})
                    new_mr.state_event = "close"
                    new_mr.save()
                elif gl_state == "closed":
                    new_mr.state_event = "close"
                    new_mr.save()
        except Exception as e:
            console.print(f"    [yellow]PR '{title}': {e}[/]")


# ─── Project Restore ──────────────────────────────────────────────────────────

def restore_project(gl, proj_dir: Path, group_obj, cfg: dict, original_root: str) -> dict:
    start = time.time()
    result = {
        "path": str(proj_dir.relative_to(cfg["backup_dir"] / "groups")),
        "status": "ok",
        "errors": [],
    }

    meta = read_json(proj_dir / "project_meta.json")
    if meta is None:
        result["status"] = "error"
        result["errors"].append("project_meta.json missing")
        return result

    # Bitbucket repo metadata fields
    project_name = meta.get("name", proj_dir.name)
    project_path = meta.get("slug", proj_dir.name)
    is_private = meta.get("is_private", True)
    description = meta.get("description") or ""

    target_full_path = f"{group_obj.full_path}/{project_path}" if group_obj else project_path

    existing_project = None
    try:
        existing_project = gl.projects.get(target_full_path)
        if not cfg["force"]:
            console.print(f"  [yellow]EXISTS[/]   {target_full_path} (use --force to overwrite metadata)")
            result["status"] = "skipped"
            result["duration_seconds"] = round(time.time() - start, 1)
            return result
    except gitlab.exceptions.GitlabGetError:
        pass

    project = existing_project
    if project is None:
        try:
            create_params = {
                "name": project_name,
                "path": project_path,
                "description": description,
                "visibility": "private",  # Always private to match parent group
                "initialize_with_readme": False,
            }
            if group_obj:
                create_params["namespace_id"] = group_obj.id
            if not cfg["dry_run"]:
                project = with_retry(lambda p=create_params: gl.projects.create(p))
                console.print(f"  [green]CREATED[/]  {target_full_path}")
            else:
                console.print(f"  [dim][dry-run] Would create project: {target_full_path}[/]")
        except Exception as e:
            result["status"] = "error"
            result["errors"].append(f"create project: {e}")
            return result

    if cfg["dry_run"]:
        result["duration_seconds"] = round(time.time() - start, 1)
        return result

    # Push git mirror (includes all branches)
    repo_git = proj_dir / "repo.git"
    if repo_git.exists():
        token = cfg["token"]
        remote_url = project.http_url_to_repo.replace("https://", f"https://oauth2:{token}@")
        ok = push_mirror(repo_git, remote_url, cfg["dry_run"])
        if not ok:
            result["errors"].append("git push --mirror failed")

    # Restore pull requests with comments
    prs = read_json(proj_dir / "merge_requests.json") or []
    restore_merge_requests(project, prs, cfg["dry_run"])

    if result["errors"]:
        result["status"] = "partial"
    result["duration_seconds"] = round(time.time() - start, 1)
    return result


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bitbucket → GitLab Restore Tool")
    parser.add_argument("--backup-dir", required=True,
                        help="Path to a snapshot directory (e.g. backups/2026-03-10T14-32-00)")
    parser.add_argument("--target-group", help="Override RESTORE_TARGET_GROUP from .env")
    parser.add_argument("--force", action="store_true",
                        help="Re-apply metadata even if project/group already exists")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without making changes")
    args = parser.parse_args()

    cfg = load_config(args)

    console.print(Panel.fit(
        f"[bold cyan]Bitbucket → GitLab Restore[/]\n"
        f"GitLab instance : {cfg['url']}\n"
        f"Target group    : {cfg['target_group']}\n"
        f"Backup dir      : {cfg['backup_dir']}\n"
        f"Force           : {cfg['force']}\n"
        f"Dry-run         : {cfg['dry_run']}",
        title="Configuration"
    ))

    started_at = datetime.now(timezone.utc)

    gl = gitlab.Gitlab(cfg["url"], private_token=cfg["token"])
    gl.auth()

    # Determine original workspace root from backup log or directory
    backup_log = read_json(cfg["backup_dir"] / "backup_log.json")
    if backup_log and backup_log.get("groups"):
        original_root = backup_log["groups"][0]
    else:
        groups_dir = cfg["backup_dir"] / "groups"
        children = [d for d in groups_dir.iterdir() if d.is_dir()] if groups_dir.exists() else []
        original_root = children[0].name if children else ""

    console.print(f"Original Bitbucket workspace: [cyan]{original_root}[/]")
    console.print(f"Restore target GitLab group:  [cyan]{cfg['target_group']}[/]\n")

    # Step 1: Restore group tree (workspace → group, BB projects → subgroups)
    restored_groups = restore_group_tree(gl, cfg["backup_dir"], cfg["target_group"], cfg["dry_run"])
    console.print(f"  Groups processed: {len(restored_groups)}\n")

    # Step 2: Restore repositories as GitLab projects
    groups_root = cfg["backup_dir"] / "groups"
    project_dirs = [p.parent for p in groups_root.rglob("project_meta.json")]
    console.print(f"[bold]Restoring {len(project_dirs)} repositories...[/]")

    project_results = []
    for proj_dir in project_dirs:
        rel = proj_dir.relative_to(groups_root)
        group_rel_parts = rel.parts[:-1]

        if group_rel_parts:
            original_group_path = "/".join(group_rel_parts)
            group_obj = restored_groups.get(original_group_path)
            if group_obj is None and not cfg["dry_run"]:
                new_group_path = remap_path(original_group_path, original_root, cfg["target_group"])
                try:
                    group_obj = gl.groups.get(new_group_path)
                except Exception:
                    console.print(f"  [red]Cannot find target group for {proj_dir.name}, skipping[/]")
                    continue
        else:
            group_obj = None

        result = restore_project(gl, proj_dir, group_obj, cfg, original_root)
        project_results.append(result)
        if result["status"] not in ("skipped",):
            for err in result.get("errors", []):
                console.print(f"    [dim]! {err}[/]")

    # Summary
    finished_at = datetime.now(timezone.utc)
    duration = round((finished_at - started_at).total_seconds(), 1)

    ok_count      = sum(1 for r in project_results if r["status"] == "ok")
    skipped_count = sum(1 for r in project_results if r["status"] == "skipped")
    error_count   = sum(1 for r in project_results if r["status"] == "error")
    partial_count = sum(1 for r in project_results if r["status"] == "partial")

    console.print()
    table = Table(title="Restore Summary", show_header=True, header_style="bold cyan")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    table.add_row("Duration", f"{duration}s ({duration / 60:.1f} min)")
    table.add_row("Groups restored", str(len(restored_groups)))
    table.add_row("Repositories OK", str(ok_count))
    table.add_row("Repositories partial", str(partial_count))
    table.add_row("Repositories skipped", str(skipped_count))
    table.add_row("Repositories errored", str(error_count))
    console.print(table)

    sys.exit(0 if error_count == 0 else 1)


if __name__ == "__main__":
    main()