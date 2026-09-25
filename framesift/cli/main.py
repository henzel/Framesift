"""`framesift` CLI: scan, classify, report, apply, undo, purge, status, gui.

Console output is compact (totals and up to 5 examples per category); full lists go to
<Review>/.framesift/reports/, progress to <Review>/.framesift/logs/.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from framesift import __version__
from framesift.engine import api
from framesift.engine.catalog import CatalogError, LockHeld
from framesift.engine.config import CATEGORY_ORDER, ClassifyConfig
from framesift.engine.i18n import set_language, t
from framesift.engine.jobs import setup_logging
from framesift.engine.reports import (
    report_dir,
    write_candidate_lists,
    write_journal_csv,
    write_json,
)

app = typer.Typer(
    name="framesift",
    help="Sort large photo/video archives safely. Nothing is deleted except by `purge`; every move is undoable.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

EXIT_ERROR, EXIT_USAGE, EXIT_LOCK, EXIT_REFUSED, EXIT_PARTIAL = 1, 2, 3, 4, 5

SourceOpt = Annotated[
    Path | None, typer.Option("--source", "-s", help="Source folder with photos and videos.")
]
CatalogOpt = Annotated[
    Path | None,
    typer.Option(
        "--catalog",
        "-c",
        help="Review folder holding .framesift/ (default: <source>/_framesift_review).",
    ),
]
DeleteOpt = Annotated[
    Path | None,
    typer.Option("--delete-dir", help="Delete folder (default: <source>/_framesift_delete)."),
]
RecursiveOpt = Annotated[
    bool, typer.Option("--recursive/--no-recursive", help="Include subfolders of the source.")
]
WorkersOpt = Annotated[
    int | None, typer.Option("--workers", "-w", help="Processes for the heavy stages.")
]
LowPrioOpt = Annotated[
    bool, typer.Option("--low-priority", help="nice/ionice this process and its workers.")
]
LogOpt = Annotated[
    Path | None,
    typer.Option("--log-file", help="Progress log (default: <catalog>/logs/framesift-<date>.log)."),
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print a JSON summary instead of text.")]
LangOpt = Annotated[str | None, typer.Option("--lang", help="Message language: en or ru.")]
YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Really do it (default is a dry run).")]
ForceLockOpt = Annotated[bool, typer.Option("--force-lock", help="Take over a stale catalog lock.")]


def human_bytes(n: int | float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _echo(text: str = "") -> None:
    typer.echo(text)


def _fail(message: str, code: int = EXIT_ERROR) -> None:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def _open(
    source: Path | None,
    catalog: Path | None,
    delete: Path | None,
    recursive: bool = True,
    *,
    force_lock: bool = False,
    lang: str | None = None,
    log_file: Path | None = None,
    read_only: bool = False,
) -> api.Workspace:
    set_language(lang)
    try:
        ws = api.open_workspace(
            source,
            catalog,
            delete,
            include_subfolders=recursive,
            force_lock=force_lock,
            read_only=read_only,
            app=f"cli {__version__}",
        )
    except api.WorkspaceError as exc:
        for issue in exc.issues:
            typer.echo(f"error: {t(issue.key, detail=issue.detail)}", err=True)
        raise typer.Exit(EXIT_REFUSED) from None
    except LockHeld as exc:
        typer.echo(f"error: {exc}. Use --force-lock if that process is gone.", err=True)
        raise typer.Exit(EXIT_LOCK) from None
    except CatalogError as exc:
        _fail(str(exc))
        raise  # pragma: no cover
    for issue in ws.issues:
        if issue.level in ("warning", "info"):
            typer.echo(f"{issue.level}: {t(issue.key, detail=issue.detail)}", err=True)
    if not read_only:
        setup_logging(
            log_file or ws.catalog.dir / "logs" / f"framesift-{datetime.now(UTC):%Y%m%d}.log"
        )
    return ws


def _cfg_overrides(
    cfg: ClassifyConfig,
    categories: str | None,
    short_video_seconds: float | None,
    similar_window: float | None,
    similar_distance: int | None,
    blur_threshold: float | None,
    dark_threshold: float | None,
    move_all_aae: bool | None,
) -> ClassifyConfig:
    cfg = cfg.with_overrides(
        short_video_seconds=short_video_seconds,
        similar_window_seconds=similar_window,
        similar_distance=similar_distance,
        blur_threshold=blur_threshold,
        dark_mean=dark_threshold,
        move_all_aae=move_all_aae,
    )
    if categories:
        wanted = {c.strip() for c in categories.split(",") if c.strip()}
        unknown = wanted - set(CATEGORY_ORDER)
        if unknown:
            _fail(f"unknown categories: {', '.join(sorted(unknown))}", EXIT_USAGE)
        cfg.enabled = {c: c in wanted for c in CATEGORY_ORDER}
    return cfg


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    if version:
        _echo(f"framesift {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        _echo(ctx.get_help())
        raise typer.Exit()


@app.command()
def scan(
    source: SourceOpt = None,
    catalog: CatalogOpt = None,
    delete_dir: DeleteOpt = None,
    recursive: RecursiveOpt = True,
    workers: WorkersOpt = None,
    low_priority: LowPrioOpt = False,
    log_file: LogOpt = None,
    json_out: JsonOpt = False,
    lang: LangOpt = None,
    force_lock: ForceLockOpt = False,
) -> None:
    """Index the source folder: files, metadata, Live Photo pairs. Incremental and safe to repeat."""
    if source is None and catalog is None:
        _fail("pass --source (first run) or --catalog", EXIT_USAGE)
    with _open(
        source, catalog, delete_dir, recursive, force_lock=force_lock, lang=lang, log_file=log_file
    ) as ws:
        stats = api.run_scan(ws, low_priority=low_priority, threads=workers)
        folder = report_dir(ws.catalog.dir, "scan")
        write_json(
            folder, "report.json", {"scan": stats.as_dict(), "roots": api.status(ws)["roots"]}
        )
        if json_out:
            _echo(json.dumps({"scan": stats.as_dict(), "report": str(folder)}, ensure_ascii=False))
        else:
            _echo(
                f"Scanned {stats.files_seen:,} files ({human_bytes(stats.bytes)}) in {stats.dirs:,} folders: "
                f"{stats.new:,} new, {stats.changed:,} changed, {stats.unchanged:,} unchanged, {stats.missing:,} missing."
            )
            _echo(
                f"Metadata: {stats.meta_extracted:,} extracted, {stats.meta_errors:,} unreadable. Skipped folders: {stats.skipped_dirs}. Errors: {stats.errors}."
            )
            _echo(f"Report: {folder}")
        if stats.errors or stats.meta_errors:
            raise typer.Exit(EXIT_PARTIAL)


@app.command()
def classify(
    source: SourceOpt = None,
    catalog: CatalogOpt = None,
    delete_dir: DeleteOpt = None,
    categories: Annotated[
        str | None,
        typer.Option("--categories", help="Comma-separated subset of: " + ",".join(CATEGORY_ORDER)),
    ] = None,
    short_video_seconds: Annotated[float | None, typer.Option("--short-video-seconds")] = None,
    similar_window: Annotated[
        float | None,
        typer.Option(
            "--similar-window", help="Seconds around each shot to look for similar frames."
        ),
    ] = None,
    similar_distance: Annotated[
        int | None, typer.Option("--similar-distance", help="Max pHash Hamming distance (0-64).")
    ] = None,
    blur_threshold: Annotated[float | None, typer.Option("--blur-threshold")] = None,
    dark_threshold: Annotated[
        float | None,
        typer.Option(
            "--dark-threshold", help="Mean brightness (0-1) below which a frame is 'dark'."
        ),
    ] = None,
    move_all_aae: Annotated[bool | None, typer.Option("--move-all-aae/--keep-aae")] = None,
    workers: WorkersOpt = None,
    low_priority: LowPrioOpt = False,
    log_file: LogOpt = None,
    json_out: JsonOpt = False,
    lang: LangOpt = None,
    force_lock: ForceLockOpt = False,
) -> None:
    """Compute hashes and pixel metrics as needed, then build the candidate list (no files are moved)."""
    with _open(
        source, catalog, delete_dir, force_lock=force_lock, lang=lang, log_file=log_file
    ) as ws:
        cfg = _cfg_overrides(
            ws.config(),
            categories,
            short_video_seconds,
            similar_window,
            similar_distance,
            blur_threshold,
            dark_threshold,
            move_all_aae,
        )
        stats = api.run_classify(ws, cfg, workers=workers, low_priority=low_priority)
        plan = api.get_plan(ws, cfg, examples=5, lang=lang)
        folder = report_dir(ws.catalog.dir, "classify")
        write_json(folder, "report.json", {"classify": stats.as_dict(), "plan": plan.as_dict()})
        write_candidate_lists(ws.catalog, folder, plan.run_id, lang)
        if json_out:
            _echo(
                json.dumps(
                    {"classify": stats.as_dict(), "plan": plan.as_dict(), "report": str(folder)},
                    ensure_ascii=False,
                    default=str,
                )
            )
        else:
            _echo(
                f"Analyzed {stats.files:,} files: {stats.partial_hashed:,} partial hashes, {stats.full_hashed:,} full hashes, {stats.analyzed:,} pixel analyses."
            )
            _print_plan(plan, examples=5)
            _echo(f"Report: {folder}")


@app.command()
def report(
    source: SourceOpt = None,
    catalog: CatalogOpt = None,
    delete_dir: DeleteOpt = None,
    examples: Annotated[
        int, typer.Option("--examples", help="Examples per category on the console.")
    ] = 5,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out", help="Where to write the full report (default: next to the catalog)."
        ),
    ] = None,
    json_out: JsonOpt = False,
    lang: LangOpt = None,
) -> None:
    """Show the dry-run report from the last classification, without rescanning."""
    with _open(source, catalog, delete_dir, lang=lang, read_only=True) as ws:
        plan = api.get_plan(ws, examples=max(examples, 12), lang=lang)
        folder = out or report_dir(ws.catalog.dir, "report")
        if out:
            out.mkdir(parents=True, exist_ok=True)
        write_json(folder, "report.json", {"plan": plan.as_dict()})
        write_candidate_lists(ws.catalog, folder, plan.run_id, lang)
        if json_out:
            _echo(
                json.dumps(
                    {"plan": plan.as_dict(), "report": str(folder)}, ensure_ascii=False, default=str
                )
            )
        else:
            _print_plan(plan, examples=examples)
            _echo(f"Report: {folder}")


def _print_plan(plan: Any, examples: int = 5) -> None:
    if plan.run_id is None:
        _echo("No classification yet. Run `framesift classify` first.")
        return
    _echo(f"{'category':<20}{'count':>8}{'size':>12}  {'confidence':<10} enabled")
    for cat in plan.categories:
        _echo(
            f"{cat.category:<20}{cat.count:>8,}{human_bytes(cat.bytes):>12}  {t('confidence.' + cat.confidence):<10} {'yes' if cat.enabled else 'no'}"
        )
        for ex in cat.examples[:examples]:
            _echo(f"    {ex.rel_path}  ({human_bytes(ex.size)}) — {ex.reason}")
    _echo(
        f"{'total (enabled)':<20}{plan.totals['candidates']:>8,}{human_bytes(plan.totals['bytes']):>12}"
    )
    _echo(
        f"Report only: Live Photo videos {plan.live_photos['count']:,} ({human_bytes(plan.live_photos['bytes'])}); "
        f"edited pairs {plan.edited_pairs['count']:,} ({human_bytes(plan.edited_pairs['bytes'])}); "
        f"largest videos listed: {len(plan.large_videos)}"
    )
    for v in plan.large_videos[:examples]:
        est = (
            f", HEVC could save ~{human_bytes(v['hevc_savings_estimate'])}"
            if v["hevc_savings_estimate"]
            else ""
        )
        _echo(
            f"    {v['rel_path']}  {human_bytes(v['size'])} {v['codec'] or '?'} {v['width'] or '?'}x{v['height'] or '?'} {human_bytes((v['bitrate'] or 0) / 8)}/s{est}"
        )


@app.command()
def apply(
    source: SourceOpt = None,
    catalog: CatalogOpt = None,
    delete_dir: DeleteOpt = None,
    categories: Annotated[
        str | None,
        typer.Option(
            "--categories", help="Comma-separated categories to apply (default: all enabled)."
        ),
    ] = None,
    live_photos: Annotated[
        bool,
        typer.Option(
            "--live-photos",
            help="Also move every Live Photo video to Review (photos become still).",
        ),
    ] = False,
    yes: YesOpt = False,
    log_file: LogOpt = None,
    json_out: JsonOpt = False,
    lang: LangOpt = None,
    force_lock: ForceLockOpt = False,
) -> None:
    """Move candidates into <Review>/<category>/…  Dry run unless --yes."""
    with _open(
        source, catalog, delete_dir, force_lock=force_lock, lang=lang, log_file=log_file
    ) as ws:
        cats = [c.strip() for c in categories.split(",")] if categories else None
        if cats and (unknown := set(cats) - set(CATEGORY_ORDER)):
            _fail(f"unknown categories: {', '.join(sorted(unknown))}", EXIT_USAGE)
        stats = api.run_apply(ws, categories=cats, dry_run=not yes)
        live = api.run_live_photos(ws, dry_run=not yes) if live_photos else None
        folder = report_dir(ws.catalog.dir, "apply")
        write_json(
            folder,
            "report.json",
            {"apply": stats.as_dict(), "live_photos": live.as_dict() if live else None},
        )
        if yes:
            write_journal_csv(ws.catalog, folder, ws.session_id)
        if json_out:
            _echo(
                json.dumps(
                    {
                        "apply": stats.as_dict(),
                        "live_photos": live.as_dict() if live else None,
                        "report": str(folder),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            prefix = "Moved" if yes else "DRY RUN: would move"
            _echo(
                f"{prefix} {stats.items:,} items / {stats.files:,} files ({human_bytes(stats.bytes)}) to Review; {stats.conflicts} name conflicts, {len(stats.errors)} errors."
            )
            for cat, n in sorted(stats.per_category.items()):
                _echo(f"    {cat:<20}{n:>8,}")
            if live:
                _echo(f"{prefix} {live.files:,} Live Photo videos ({human_bytes(live.bytes)}).")
            if not yes:
                _echo("Add --yes to apply.")
            _echo(f"Report: {folder}")
        if stats.errors:
            raise typer.Exit(EXIT_PARTIAL)


@app.command()
def undo(
    source: SourceOpt = None,
    catalog: CatalogOpt = None,
    delete_dir: DeleteOpt = None,
    last: Annotated[bool, typer.Option("--last", help="Undo the most recent action.")] = False,
    session: Annotated[
        int | None,
        typer.Option(
            "--session", help="Undo everything done in a session id (default: the last one)."
        ),
    ] = None,
    all_sessions: Annotated[bool, typer.Option("--session-last", hidden=True)] = False,
    category: Annotated[
        str | None, typer.Option("--category", help="Undo every move of one category.")
    ] = None,
    everything: Annotated[
        bool, typer.Option("--all", help="Undo every move recorded in the journal.")
    ] = False,
    yes: YesOpt = False,
    log_file: LogOpt = None,
    json_out: JsonOpt = False,
    lang: LangOpt = None,
    force_lock: ForceLockOpt = False,
) -> None:
    """Reverse moves from the journal. Dry run unless --yes."""
    scope = "last" if last else "category" if category else "all" if everything else "session"
    if not (last or category or everything or session is not None):
        _fail("choose one of --last, --session ID, --category NAME, --all", EXIT_USAGE)
    with _open(
        source, catalog, delete_dir, force_lock=force_lock, lang=lang, log_file=log_file
    ) as ws:
        stats = api.run_undo(ws, scope, session_id=session, category=category, dry_run=not yes)
        if json_out:
            _echo(json.dumps({"undo": stats.as_dict()}, ensure_ascii=False))
        else:
            prefix = "Undone" if yes else "DRY RUN: would undo"
            _echo(
                f"{prefix} {stats.ops:,} operations / {stats.files:,} files ({human_bytes(stats.bytes)}); skipped {stats.skipped}, errors {len(stats.errors)}."
            )
            for e in stats.errors[:5]:
                _echo(f"    {e}")
            if not yes:
                _echo("Add --yes to undo.")
        if stats.errors:
            raise typer.Exit(EXIT_PARTIAL)


@app.command()
def purge(
    source: SourceOpt = None,
    catalog: CatalogOpt = None,
    delete_dir: DeleteOpt = None,
    yes: YesOpt = False,
    permanent: Annotated[
        bool,
        typer.Option("--permanent", help="Delete permanently instead of using the system trash."),
    ] = False,
    log_file: LogOpt = None,
    json_out: JsonOpt = False,
    lang: LangOpt = None,
    force_lock: ForceLockOpt = False,
) -> None:
    """Empty the Delete folder. THE ONLY COMMAND THAT DELETES FILES. Dry run unless --yes."""
    with _open(
        source, catalog, delete_dir, force_lock=force_lock, lang=lang, log_file=log_file
    ) as ws:
        preview = api.status(ws)["purge"]
        use_trash = None if not permanent else False
        if preview["network"] and not json_out:
            typer.echo(t("purge.network_warning"), err=True)
        stats = api.run_purge(ws, dry_run=not yes, use_trash=use_trash)
        if json_out:
            _echo(json.dumps({"purge": stats.as_dict()}, ensure_ascii=False))
        else:
            prefix = "Deleted" if yes else "DRY RUN: would delete"
            _echo(
                f"{prefix} {stats.files:,} files ({human_bytes(stats.bytes)}) via {stats.method}; errors {len(stats.errors)}."
            )
            if not yes:
                _echo("Add --yes to delete. This cannot be undone.")
        if stats.errors:
            raise typer.Exit(EXIT_PARTIAL)


@app.command()
def status(
    source: SourceOpt = None,
    catalog: CatalogOpt = None,
    delete_dir: DeleteOpt = None,
    json_out: JsonOpt = False,
    lang: LangOpt = None,
) -> None:
    """Catalog summary: counts per folder, review categories, recent jobs, lock owner."""
    with _open(source, catalog, delete_dir, lang=lang, read_only=True) as ws:
        info = api.status(ws)
        if json_out:
            _echo(json.dumps(info, ensure_ascii=False, default=str))
            return
        _echo(
            f"framesift {info['version']}  catalog {info['catalog']} (schema v{info['schema_version']})"
        )
        for root, stats in info["files"].items():
            _echo(f"  {root:<8}{stats['files']:>10,} files {human_bytes(stats['bytes']):>12}")
        for cat in info["review_categories"]:
            _echo(
                f"    review/{cat['category'] or '(root)':<16}{cat['files']:>8,} {human_bytes(cat['bytes']):>10}"
            )
        if info["unfinished_jobs"]:
            for j in info["unfinished_jobs"]:
                _echo(f"  unfinished job #{j['id']} {j['kind']} ({j['state']})")
        if info["lock"]:
            _echo(f"  lock: {info['lock'].get('host')} pid {info['lock'].get('pid')}")


@app.command()
def gui(
    folder: Annotated[Path | None, typer.Argument(help="Source folder to open.")] = None,
) -> None:
    """Start the graphical interface (requires the [gui] extra)."""
    try:
        from framesift.gui.app import main as gui_main
    except ImportError:
        _fail("the GUI is not installed: pip install 'framesift[gui]'")
        return
    sys.exit(gui_main([str(folder)] if folder else []))


def main() -> None:
    # Windows consoles and redirected output may not be UTF-8; never crash on a reason string
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
