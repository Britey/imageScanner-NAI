from __future__ import annotations

import argparse
from pathlib import Path

from .cluster import build_clusters
from .config import ScanConfig
from .db import checkpoint_database, connect, init_db
from .failures import iter_failures
from .model_paths import (
    DEFAULT_MODEL_PATHS_PATH,
    build_config,
    load_model_paths,
    make_rule,
    save_model_paths,
)
from .query import query_image, query_prompt_tags, write_query_html
from .review import generate_review
from .scan import scan_roots
from .single_instance import SingleInstanceLock, notify_already_running
from .web import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="imgdupe")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Index image files into SQLite.")
    scan_parser.add_argument("roots", nargs="+", type=Path)
    scan_parser.add_argument("--db", type=Path, required=True)
    scan_parser.add_argument("--workers", type=int, default=None)
    scan_parser.add_argument("--batch-size", type=int, default=None)
    scan_parser.add_argument("--crop-index", action="store_true")
    scan_parser.add_argument("--no-prompt-index", action="store_true")
    scan_parser.add_argument("--stealth-prompt", action="store_true")
    scan_parser.add_argument("--reindex-prompts", action="store_true")
    scan_parser.add_argument("--delete-missing", action="store_true")
    scan_parser.add_argument(
        "--model-label",
        help="Save this arbitrary model label for each otherwise-unconfigured scan root.",
    )

    query_parser = subparsers.add_parser("query", help="Find visually similar images.")
    query_parser.add_argument("image", type=Path)
    query_parser.add_argument("--db", type=Path, required=True)
    query_parser.add_argument("--html", type=Path)
    query_parser.add_argument("--limit", type=int, default=50)
    query_parser.add_argument("--min-score", type=float, default=0.0)
    query_parser.add_argument("--hide-exact", action="store_true")
    query_parser.add_argument("--tryhard", action="store_true")

    tags_parser = subparsers.add_parser("tags", help="Find images by indexed prompt or model tags.")
    tags_parser.add_argument("tags", nargs="*", help="Prompt tag(s), comma/CJK-comma/newline separated.")
    tags_parser.add_argument("--db", type=Path, required=True)
    tags_parser.add_argument("--mode", choices=("all", "any"), default="all")
    tags_parser.add_argument(
        "--sort",
        choices=(
            "created-desc",
            "created-asc",
            "modified-desc",
            "modified-asc",
            "name-asc",
            "name-desc",
        ),
        default="created-desc",
        help="Secondary sort for prompt tag results after matching-tag count.",
    )
    tags_parser.add_argument(
        "--not-tags",
        nargs="+",
        default=[],
        help="Prompt tag(s) to exclude, comma/CJK-comma/newline separated.",
    )
    tags_parser.add_argument("--limit", type=int, default=100)
    tags_parser.add_argument("--offset", type=int, default=0)
    tags_parser.add_argument(
        "--model-type",
        default="all",
        help="Detected or configured model label (for example nai4, noob75, or sd1.5).",
    )

    cluster_parser = subparsers.add_parser("cluster", help="Build similar-image groups.")
    cluster_parser.add_argument("--db", type=Path, required=True)
    cluster_parser.add_argument("--min-score", type=float, default=70.0)

    review_parser = subparsers.add_parser("review", help="Generate static cluster review pages.")
    review_parser.add_argument("--db", type=Path, required=True)
    review_parser.add_argument("--out", type=Path, required=True)
    review_parser.add_argument("--thumbnail-size", type=int, default=256)

    serve_parser = subparsers.add_parser("serve", help="Run a local web UI for image search.")
    serve_parser.add_argument("--db", type=Path, required=True)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--limit", type=int, default=100)
    serve_parser.add_argument("--min-score", type=float, default=55.0)
    serve_parser.add_argument("--thumbnail-size", type=int, default=256)
    serve_parser.add_argument("--open-browser", action="store_true")

    failures_parser = subparsers.add_parser("failures", help="Show image indexing failures.")
    failures_parser.add_argument("--db", type=Path, required=True)
    failures_parser.add_argument("--limit", type=int, default=100)

    args = parser.parse_args(argv)
    if args.command in {"query", "tags"} and args.limit <= 0:
        parser.error("--limit must be greater than zero")

    instance_lock = SingleInstanceLock() if args.command == "serve" else None
    if instance_lock is not None and not instance_lock.acquire():
        notify_already_running()
        return 1
    try:
        conn = connect(args.db)
        try:
            init_db(conn)
            return _run_command(args, parser, conn)
        finally:
            checkpoint = checkpoint_database(conn, "TRUNCATE")
            if checkpoint and checkpoint[0]:
                print(f"WAL checkpoint remained busy: {checkpoint}")
            conn.close()
    finally:
        if instance_lock is not None:
            instance_lock.release()


def _run_command(args, parser: argparse.ArgumentParser, conn) -> int:

    if args.command == "scan":
        default_scan_config = ScanConfig()
        scan_config = ScanConfig(
            workers=args.workers if args.workers is not None else default_scan_config.workers,
            batch_size=args.batch_size if args.batch_size is not None else default_scan_config.batch_size,
            crop_index=args.crop_index,
            prompt_index=not args.no_prompt_index,
            stealth_prompt=args.stealth_prompt,
            reindex_prompts=args.reindex_prompts,
            delete_missing=args.delete_missing,
        )
        model_paths = load_model_paths(DEFAULT_MODEL_PATHS_PATH)
        if args.model_label:
            if model_paths.issues:
                parser.error("model_paths.ini contains invalid rules; fix it before adding labels")
            additions = []
            added_targets: set[str] = set()
            for supplied in args.roots:
                canonical = supplied.resolve(strict=False)
                target = canonical if canonical.is_dir() else canonical.parent
                target_key = str(target).casefold()
                if model_paths.resolve(target) is None and target_key not in added_targets:
                    additions.append(make_rule(target, args.model_label))
                    added_targets.add(target_key)
            if additions:
                model_paths = build_config((*model_paths.rules, *additions))
                save_model_paths(DEFAULT_MODEL_PATHS_PATH, model_paths)
        stats = scan_roots(conn, args.roots, config=scan_config, model_paths=model_paths)
        print(
            "scan complete: "
            f"seen={stats.seen} queued={stats.queued} indexed={stats.indexed} "
            f"moved={stats.moved} deleted={stats.deleted} skipped={stats.skipped} failed={stats.failed}"
        )
        return 0

    if args.command == "tags":
        if not args.tags and not args.not_tags:
            parser.error("tags requires at least one tag or --not-tags value")
        results = query_prompt_tags(
            conn,
            list(args.tags),
            negative_text_or_tags=list(args.not_tags),
            match_mode=args.mode,
            limit=args.limit,
            offset=args.offset,
            sort_by=args.sort.replace("-", "_"),
            model_type=args.model_type,
        )
        for result in results:
            image_row = result.image_row
            print(
                f"matches={result.matched_count:2d} "
                f"{image_row['width']}x{image_row['height']} "
                f"{image_row['path']}"
            )
        print(f"tag results: {len(results)}")
        return 0

    if args.command == "query":
        query_path = args.image.resolve()
        results = query_image(
            conn,
            query_path,
            limit=args.limit,
            min_score=args.min_score,
            include_exact=not args.hide_exact,
            tryhard=args.tryhard,
        )
        for image_row, candidate, score in results:
            print(
                f"{score.score:6.2f} {score.decision:18} "
                f"bands={candidate.total_hits:3d} "
                f"phash={score.phash_dist} whash={score.whash_dist} "
                f"dhash={score.dhash_dist} grid={score.grid_match_count} "
                f"crop={score.crop_min_dist} "
                f"{image_row['path']}"
            )
        if args.html:
            write_query_html(args.html, query_path, results)
            print(f"wrote {args.html}")
        return 0

    if args.command == "cluster":
        stats = build_clusters(conn, min_score=args.min_score)
        print(
            "cluster complete: "
            f"images={stats.images} candidate_pairs={stats.candidate_pairs} "
            f"scored_pairs={stats.scored_pairs} stored_matches={stats.stored_matches} "
            f"clusters={stats.clusters} clustered_images={stats.clustered_images}"
        )
        return 0

    if args.command == "review":
        stats = generate_review(
            conn,
            args.out,
            thumbnail_size=args.thumbnail_size,
        )
        print(
            "review complete: "
            f"clusters={stats.clusters} images={stats.images} "
            f"thumbnails={stats.thumbnails} out={stats.out_dir}"
        )
        return 0

    if args.command == "serve":
        serve(
            conn,
            args.db,
            host=args.host,
            port=args.port,
            limit=args.limit,
            min_score=args.min_score,
            thumbnail_size=args.thumbnail_size,
            open_browser=args.open_browser,
        )
        return 0

    # argparse only accepts the commands handled above and this final command.
    rows = iter_failures(conn, limit=args.limit)
    for row in rows:
        print(f"{row['path']}\t{row['decode_error']}")
    print(f"failures: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
