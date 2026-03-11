#!/usr/bin/env python3
"""
Bitbucket Workspace Backup Tool
    Eugen Marin, 2024-06-10 - ARRC DevOps

Backs up a Bitbucket Cloud workspace (projects, repositories, pull requests,
and comments) to a local snapshot directory for GitLab restoration.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

load_dotenv()
console = Console()

BB_API = "https://api.bitbucket.org/2.0"
REPOS_FILTER_FILE = Path(__file__).parent / "repos_filter.txt"


# ─── Repository Filter ────────────────────────────────────────────────────────

def load_repos_filter() -> dict:
    """
    Read repos_filter.txt and return a dict with two sets:
      - "projects": Bitbucket project keys to include (all their repos)
      - "slugs":    individual repository slugs to include
    Lines starting with '#' and blank lines are ignored.
    Both sets empty means back up everything.

    Syntax:
      project:<KEY>   — include all repos in that Bitbucket project
      <repo-slug>     — include a single repo by slug
    """
    result = {"projects": set(), "slugs": set()}
    if not REPOS_FILTER_FILE.exists():
        return result
    for line in REPOS_FILTER_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("project:"):
            key = line[len("project:"):].strip().upper()
            if key:
                result["projects"].add(key)
        else:
            result["slugs"].add(line)
    return result


# ─── Configuration ────────────────────────────────────────────────────────────

def load_config(args):
    cfg = {
        "workspace":    (args.workspace or os.getenv("BITBUCKET_WORKSPACE", "")).strip(),
        "username":     os.getenv("BITBUCKET_USERNAME", "").strip(),
        "app_password": os.getenv("BITBUCKET_APP_PASSWORD", "").strip(),
        "backup_dir":   Path(args.output_dir or os.getenv("BACKUP_DIR", "./backups")),
        "workers":      int(os.getenv("BACKUP_WORKERS", "4")),
        "dry_run":      args.dry_run,
        "repos_filter": load_repos_filter(),
    }
    if not cfg["workspace"]:
        console.print("[bold red]ERROR:[/] BITBUCKET_WORKSPACE is not set.")
        sys.exit(1)
    if not cfg["username"] or not cfg["app_password"]:
        console.print("[bold red]ERROR:[/] BITBUCKET_USERNAME and BITBUCKET_APP_PASSWORD must both be set.")
        sys.exit(1)
    cfg["auth"] = (cfg["username"], cfg["app_password"])
    return cfg


# ─── Bitbucket API Helpers ────────────────────────────────────────────────────

def bb_get(session: requests.Session, url: str) -> dict:
    """GET a single Bitbucket API URL; raise on non-2xx."""
    resp = session.get(url)
    if not resp.ok:
        console.print(f"[bold red]API Error:[/] {resp.status_code} {resp.reason}")
        console.print(f"[dim]URL:[/dim] {url}")
        try:
            error_data = resp.json()
            console.print(f"[dim]Response:[/dim] {json.dumps(error_data, indent=2)}")
        except:
            console.print(f"[dim]Response:[/dim] {resp.text[:500]}")
    resp.raise_for_status()
    return resp.json()


def paginated_list(session: requests.Session, url: str) -> list:
    """Follow Bitbucket 'next' pagination and return all values."""
    results = []
    next_url = url
    while next_url:
        data = bb_get(session, next_url)
        results.extend(data.get("values", []))
        next_url = data.get("next")
    return results


def fetch_with_retry(fn, retries=5, backoff=10):
    """Call fn(), retrying on HTTP 429 with exponential back-off."""
    for attempt in range(retries):
        try:
            return fn()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429 and attempt < retries - 1:
                wait = backoff * (2 ** attempt)
                console.print(f"  [yellow]Rate limited — waiting {wait}s...[/]")
                time.sleep(wait)
            else:
                raise
    return []


def make_session(auth: tuple) -> requests.Session:
    session = requests.Session()
    session.auth = auth
    session.headers.update({"Accept": "application/json"})
    return session


# ─── Git Mirror Clone ─────────────────────────────────────────────────────────

def git_mirror_clone(clone_url_with_auth: str, dest: Path, dry_run: bool) -> bool:
    """Clone or update a bare mirror of a repository."""
    if dry_run:
        console.print(f"  [dim][dry-run] Would clone {dest}[/]")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    if (dest / "HEAD").exists():
        result = subprocess.run(
            ["git", "remote", "update", "--prune"],
            cwd=dest, capture_output=True, text=True
        )
    else:
        result = subprocess.run(
            ["git", "clone", "--mirror", clone_url_with_auth, str(dest)],
            capture_output=True, text=True
        )
    if result.returncode != 0:
        console.print(f"  [red]git error:[/] {result.stderr.strip()}")
        return False
    return True


# ─── JSON Helpers ─────────────────────────────────────────────────────────────

def safe_json_dump(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def sanitize_dirname(name: str) -> str:
    """Sanitize a name for use as a directory name."""
    # Replace invalid filesystem characters with hyphens
    invalid_chars = '<>:"/\\|?*'
    sanitized = name
    for char in invalid_chars:
        sanitized = sanitized.replace(char, '-')
    # Remove leading/trailing whitespace and dots
    sanitized = sanitized.strip('. ')
    # Replace multiple spaces with single hyphen
    sanitized = '-'.join(sanitized.split())
    # Limit length to avoid filesystem issues
    if len(sanitized) > 100:
        sanitized = sanitized[:100].rstrip('-')
    return sanitized.lower()


# ─── Discovery ────────────────────────────────────────────────────────────────

def discover_bb_projects(session: requests.Session, workspace: str) -> list:
    """Return all Bitbucket projects in the workspace."""
    url = f"{BB_API}/workspaces/{workspace}/projects"
    return fetch_with_retry(lambda: paginated_list(session, url))


def discover_repos(session: requests.Session, workspace: str) -> list:
    """Return all repositories in the workspace (with project info embedded)."""
    url = f"{BB_API}/repositories/{workspace}?pagelen=100"
    return fetch_with_retry(lambda: paginated_list(session, url))


# ─── Workspace & Project Metadata ─────────────────────────────────────────────

def backup_workspace_meta(session, workspace: str, snapshot_dir: Path, dry_run: bool):
    """Save workspace-level metadata and member list."""
    ws_dir = snapshot_dir / "groups" / workspace
    ws_dir.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return
    try:
        meta = bb_get(session, f"{BB_API}/workspaces/{workspace}")
        safe_json_dump(ws_dir / "group_meta.json", meta)
    except Exception as e:
        console.print(f"  [yellow]Warning:[/] workspace meta: {e}")
    try:
        members = fetch_with_retry(lambda: paginated_list(session, f"{BB_API}/workspaces/{workspace}/members"))
        safe_json_dump(ws_dir / "members.json", members)
    except Exception as e:
        console.print(f"  [yellow]Warning:[/] workspace members: {e}")


def backup_project_meta(session, workspace: str, project: dict, snapshot_dir: Path, dry_run: bool):
    """Save Bitbucket project metadata (maps to a GitLab subgroup directory)."""
    proj_name = sanitize_dirname(project.get("name", project.get("key", "unknown")))
    proj_dir = snapshot_dir / "groups" / workspace / proj_name
    proj_dir.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return
    safe_json_dump(proj_dir / "group_meta.json", project)


# ─── Per-Repository Backup ────────────────────────────────────────────────────

def backup_repo(session: requests.Session, workspace: str, repo: dict, cfg: dict, snapshot_dir: Path) -> dict:
    """Backup a single Bitbucket repository. Returns a result dict."""
    start = time.time()
    repo_slug = repo.get("slug", "")
    
    # Get project name instead of key
    project_obj = repo.get("project", {})
    project_name = project_obj.get("name", "")
    project_key = project_obj.get("key", "")
    
    if project_name:
        project_dirname = sanitize_dirname(project_name)
        display_path = f"{workspace}/{project_name}/{repo_slug}"
    else:
        project_dirname = ""
        display_path = f"{workspace}/{repo_slug}"
    
    result = {"path": display_path, "status": "ok", "errors": []}

    # Local directory
    if project_dirname:
        repo_dir = snapshot_dir / "groups" / workspace / project_dirname / repo_slug
    else:
        repo_dir = snapshot_dir / "groups" / workspace / repo_slug
    repo_dir.mkdir(parents=True, exist_ok=True)

    # 1. Repository metadata
    if not cfg["dry_run"]:
        safe_json_dump(repo_dir / "project_meta.json", repo)

    # 2. Git mirror clone (includes all branches)
    scm = repo.get("scm", "git")
    if scm != "git":
        result["errors"].append(f"Skipping git clone: unsupported SCM '{scm}' (only git is supported)")
    else:
        username = cfg["username"]
        app_password = cfg["app_password"]
        clone_url = f"https://{username}:{app_password}@bitbucket.org/{workspace}/{repo_slug}.git"
        ok = git_mirror_clone(clone_url, repo_dir / "repo.git", cfg["dry_run"])
        if not ok:
            result["errors"].append("git clone/fetch failed")

    base_url = f"{BB_API}/repositories/{workspace}/{repo_slug}"

    # 3. Pull requests with comments (saved as merge_requests.json for GitLab restore compatibility)
    if not cfg["dry_run"]:
        try:
            prs = fetch_with_retry(lambda: paginated_list(session, f"{base_url}/pullrequests?state=ALL&pagelen=50"))
            full_prs = []
            for pr in prs:
                pr_id = pr.get("id")
                try:
                    comments = fetch_with_retry(
                        lambda pid=pr_id: paginated_list(session, f"{base_url}/pullrequests/{pid}/comments")
                    )
                    pr["_notes"] = comments
                except Exception as e:
                    result["errors"].append(f"PR {pr_id} comments: {e}")
                    pr["_notes"] = []
                full_prs.append(pr)
            safe_json_dump(repo_dir / "merge_requests.json", full_prs)
        except Exception as e:
            result["errors"].append(f"pull requests: {e}")

    if result["errors"]:
        result["status"] = "partial"
    result["duration_seconds"] = round(time.time() - start, 1)
    return result


# ─── Summary & Logging ────────────────────────────────────────────────────────

def write_logs(snapshot_dir: Path, log: dict):
    safe_json_dump(snapshot_dir / "backup_log.json", log)

    summary_lines = [
        "Bitbucket Backup Summary",
        "=" * 60,
        f"Started:   {log['started_at']}",
        f"Finished:  {log['finished_at']}",
        f"Duration:  {log['duration_seconds']}s ({log['duration_seconds'] / 60:.1f} min)",
        "",
        f"Groups backed up:   {log['totals']['groups']}",
        f"Projects backed up: {log['totals']['projects']}",
        f"Pull requests:      {log['totals']['merge_requests']}",
        f"Errors:             {log['totals']['errors']}",
        "",
        "─" * 60,
        "Groups:",
    ]
    for g in log["groups"]:
        summary_lines.append(f"  {g}")

    summary_lines += ["", "─" * 60, "Repositories:"]
    for p in log["projects"]:
        status_icon = "OK" if p["status"] == "ok" else ("PARTIAL" if p["status"] == "partial" else "ERROR")
        summary_lines.append(f"  [{status_icon:7s}] {p['path']} ({p.get('duration_seconds', 0)}s)")
        for err in p.get("errors", []):
            summary_lines.append(f"           ! {err}")

    summary_text = "\n".join(summary_lines)
    (snapshot_dir / "backup_summary.txt").write_text(summary_text, encoding="utf-8")
    return summary_text


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bitbucket Workspace Backup Tool")
    parser.add_argument("--workspace", help="Override BITBUCKET_WORKSPACE from .env")
    parser.add_argument("--output-dir", help="Override BACKUP_DIR from .env")
    parser.add_argument("--dry-run", action="store_true", help="Discover and log without writing files")
    args = parser.parse_args()

    cfg = load_config(args)

    rf = cfg["repos_filter"]
    has_filter = bool(rf["projects"] or rf["slugs"])
    if has_filter:
        filter_parts = [f"project:{k}" for k in sorted(rf["projects"])] + sorted(rf["slugs"])
        filter_display = ", ".join(filter_parts)
    else:
        filter_display = "all repositories"
    console.print(Panel.fit(
        f"[bold cyan]Bitbucket Backup[/]\n"
        f"Workspace  : {cfg['workspace']}\n"
        f"Workers    : {cfg['workers']}\n"
        f"Repos      : {filter_display}\n"
        f"Dry-run    : {cfg['dry_run']}",
        title="Configuration"
    ))

    # Create snapshot directory with timestamp
    started_at = datetime.now(timezone.utc)
    ts = started_at.strftime("%Y-%m-%dT%H-%M-%S")
    snapshot_dir = cfg["backup_dir"] / ts
    if not cfg["dry_run"]:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"Snapshot dir: [green]{snapshot_dir}[/]\n")

    session = make_session(cfg["auth"])

    # Verify credentials work
    console.print("[bold]Verifying credentials...[/]")
    try:
        user_data = bb_get(session, f"{BB_API}/user")
        console.print(f"  ✓ Authenticated as: [cyan]{user_data.get('display_name', user_data.get('username', 'N/A'))}[/cyan]")
    except Exception as e:
        console.print(f"[bold red]✗ Authentication failed![/bold red]")
        console.print(f"  Username: {cfg['username']}")
        console.print(f"  App password length: {len(cfg['app_password'])} characters")
        console.print(f"[yellow]Hint:[/yellow] Verify your BITBUCKET_USERNAME and BITBUCKET_APP_PASSWORD in .env")
        console.print(f"[yellow]Note:[/yellow] Username should be your Bitbucket username (not email)")
        raise

    # Discovery
    console.print("[bold]Discovering Bitbucket projects...[/]")
    bb_projects = fetch_with_retry(lambda: discover_bb_projects(session, cfg["workspace"]))
    console.print(f"  Found [cyan]{len(bb_projects)}[/] projects")

    console.print("[bold]Discovering repositories...[/]")
    repos = fetch_with_retry(lambda: discover_repos(session, cfg["workspace"]))
    # Deduplicate by repo slug
    seen_slugs = set()
    unique_repos = []
    for r in repos:
        slug = r.get("slug", "")
        if slug not in seen_slugs:
            seen_slugs.add(slug)
            unique_repos.append(r)
    console.print(f"  Found [cyan]{len(unique_repos)}[/] repositories")

    rf = cfg["repos_filter"]
    if rf["projects"] or rf["slugs"]:
        def repo_matches(r):
            if r.get("slug", "") in rf["slugs"]:
                return True
            repo_project_key = (r.get("project", {}).get("key") or "").upper()
            return repo_project_key in rf["projects"]

        # Warn about project keys not found in workspace
        found_project_keys = {(r.get("project", {}).get("key") or "").upper() for r in unique_repos}
        unknown_projects = rf["projects"] - found_project_keys
        if unknown_projects:
            console.print(f"  [yellow]Warning:[/] project keys not found in workspace: {', '.join(sorted(unknown_projects))}")

        # Warn about individual slugs not found
        found_slugs = {r.get("slug", "") for r in unique_repos}
        unknown_slugs = rf["slugs"] - found_slugs
        if unknown_slugs:
            console.print(f"  [yellow]Warning:[/] repo slugs not found in workspace: {', '.join(sorted(unknown_slugs))}")

        unique_repos = [r for r in unique_repos if repo_matches(r)]
        console.print(f"  Filter applied — backing up [cyan]{len(unique_repos)}[/] repositories\n")
    else:
        console.print()

    # Workspace + project metadata
    console.print("[bold]Backing up workspace metadata...[/]")
    backup_workspace_meta(session, cfg["workspace"], snapshot_dir if not cfg["dry_run"] else Path("/tmp/dry"), cfg["dry_run"])

    console.print("[bold]Backing up project metadata...[/]")
    for proj in bb_projects:
        backup_project_meta(session, cfg["workspace"], proj, snapshot_dir if not cfg["dry_run"] else Path("/tmp/dry"), cfg["dry_run"])
    console.print(f"  Done ({len(bb_projects)} projects)\n")

    # Collect all group paths for log
    all_group_paths = [cfg["workspace"]] + [
        f"{cfg['workspace']}/{p.get('name', p.get('key', 'unknown'))}" for p in bb_projects
    ]

    # Backup repositories in parallel
    repo_results = []
    total_prs = 0

    console.print(f"[bold]Backing up {len(unique_repos)} repositories (workers={cfg['workers']})...[/]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Repositories", total=len(unique_repos))

        target_dir = snapshot_dir if not cfg["dry_run"] else Path("/tmp/dry")

        with ThreadPoolExecutor(max_workers=cfg["workers"]) as executor:
            futures = {
                executor.submit(backup_repo, session, cfg["workspace"], r, cfg, target_dir): r
                for r in unique_repos
            }
            for future in as_completed(futures):
                result = future.result()
                repo_results.append(result)
                status_color = "green" if result["status"] == "ok" else (
                    "yellow" if result["status"] == "partial" else "red"
                )
                progress.console.print(
                    f"  [{status_color}]{result['status'].upper():7s}[/]  "
                    f"{result['path']}  "
                    f"({result.get('duration_seconds', 0)}s)"
                )
                if result.get("errors"):
                    for err in result["errors"]:
                        progress.console.print(f"           [dim]! {err}[/]")
                progress.advance(task)

    # Tally PR counts from saved files
    if not cfg["dry_run"]:
        for repo in unique_repos:
            repo_slug = repo.get("slug", "")
            project_obj = repo.get("project", {})
            project_name = project_obj.get("name", "")
            
            if project_name:
                project_dirname = sanitize_dirname(project_name)
                repo_dir = snapshot_dir / "groups" / cfg["workspace"] / project_dirname / repo_slug
            else:
                repo_dir = snapshot_dir / "groups" / cfg["workspace"] / repo_slug
            
            mr_path = repo_dir / "merge_requests.json"
            if mr_path.exists():
                try:
                    data = json.loads(mr_path.read_text())
                    total_prs += len(data)
                except Exception:
                    pass

    finished_at = datetime.now(timezone.utc)
    duration = round((finished_at - started_at).total_seconds(), 1)

    log = {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": duration,
        "groups": all_group_paths,
        "projects": repo_results,
        "totals": {
            "groups": len(all_group_paths),
            "projects": len(unique_repos),
            "merge_requests": total_prs,
            "errors": sum(1 for r in repo_results if r["status"] == "error"),
        },
    }

    if not cfg["dry_run"]:
        write_logs(snapshot_dir, log)

    # Rich summary table
    console.print()
    table = Table(title="Backup Summary", show_header=True, header_style="bold cyan")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    table.add_row("Duration", f"{duration}s ({duration / 60:.1f} min)")
    table.add_row("Groups / Projects", str(log["totals"]["groups"]))
    table.add_row("Repositories", str(log["totals"]["projects"]))
    table.add_row("Pull Requests", str(total_prs))
    table.add_row("Errors", str(log["totals"]["errors"]))
    console.print(table)

    if not cfg["dry_run"]:
        console.print(f"\nLogs saved to [green]{snapshot_dir}[/]")

    sys.exit(0 if log["totals"]["errors"] == 0 else 1)


if __name__ == "__main__":
    main()
