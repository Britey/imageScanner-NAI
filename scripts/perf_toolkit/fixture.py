"""Deterministic, disposable SQLite fixtures for performance measurements.

These fixtures model table cardinality, skew, and row width. They do not claim
that synthetic hashes/edges are representative of any particular image corpus.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import sqlite3
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

from imgdupe.bands import GRID_HASH_TYPES, WHOLE_HASH_TYPES, split_bands
from imgdupe.db import connect, init_db
from imgdupe.hashing import compute_image_hashes
from imgdupe.nai_prompt import PROMPT_PARSER_VERSION, normalize_prompt_tag


MARKER_NAME = ".imgdupe-perf-fixture.json"
MARKER_KIND = "imgdupe-performance-fixture-v1"
BATCH_SIZE = 500


def _real_assets(directory: Path, count: int, seed: int) -> tuple[list[Path], list[dict[str, bytes]]]:
    directory.mkdir()
    rng = random.Random(seed)
    paths: list[Path] = []
    all_hashes: list[dict[str, bytes]] = []
    for index in range(count):
        # Structured colour/shape variation, reproducible without an image corpus.
        small = Image.frombytes("RGB", (64, 64), rng.randbytes(64 * 64 * 3))
        image = small.resize((512, 512), Image.Resampling.BICUBIC)
        draw = ImageDraw.Draw(image)
        for _ in range(8):
            x, y = rng.randrange(400), rng.randrange(400)
            draw.ellipse((x, y, x + rng.randrange(30, 110), y + rng.randrange(30, 110)),
                         fill=tuple(rng.randrange(256) for _ in range(3)))
        path = directory / f"image-{index + 1:06d}.png"
        image.save(path, optimize=False)
        paths.append(path)
        all_hashes.append(compute_image_hashes(path)[0])
    return paths, all_hashes


def _tags(group: int, count: int, seed: int) -> list[str]:
    rng = random.Random(seed + group * 104729)
    # A head of popular terms, plus a long tail from a 10k-name vocabulary.
    popular = ("portrait", "blue eyes", "outdoors", "smile", "long hair",
               "illustration", "soft light", "solo", "looking at viewer")
    tags = ["common"]
    tags.extend(popular[: min(len(popular), max(0, count // 3))])
    seen = set(tags)
    while len(tags) < count:
        value = f"tag_{rng.randrange(10000):05d}"
        if value not in seen:
            tags.append(value)
            seen.add(value)
    return tags


def _metadata(group: int, prompt: str, seed: int, target_bytes: int) -> tuple[bytes, int]:
    """Valid compressed JSON, using varied generation history to model width."""
    rng = random.Random(seed ^ (group * 1000003))
    value = {
        "Software": "Synthetic performance fixture",
        "prompt": prompt,
        "sampler": "k_euler_ancestral",
        "steps": 28,
        "scale": 5.0,
        "seed": rng.randrange(2**32),
        "generation_history": [],
    }
    # History is deliberately expanded synthetic provenance, not opaque padding.
    history = value["generation_history"]
    while True:
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        compressed = zlib.compress(raw)
        if len(compressed) >= target_bytes:
            return compressed, len(raw)
        missing = target_bytes - len(compressed)
        for _ in range(max(1, missing // 25)):
            history.append({
                "seed": rng.randrange(2**32),
                "variation_seed": rng.randrange(2**32),
                "strength": round(rng.random(), 8),
                "steps": rng.randrange(20, 50),
            })


def generate_fixture(
    work_dir: Path,
    image_count: int,
    seed: int = 1729,
    tags_per_image: int = 24,
    metadata_bytes: int = 2048,
) -> dict:
    """Create a self-owned fixture in a new/empty directory; never overwrite data.

    The caller owns cleanup. Only directories bearing MARKER_NAME/MARKER_KIND
    should ever be removed by automated cleanup.
    """
    if image_count < 2:
        raise ValueError("image_count must be at least 2")
    if not 2 <= tags_per_image <= 1000:
        raise ValueError("tags_per_image must be between 2 and 1000")
    if not 0 <= metadata_bytes <= 65536:
        raise ValueError("metadata_bytes must be between 0 and 65536")
    work_dir = Path(work_dir).absolute()
    if work_dir.is_symlink():
        raise ValueError("fixture directory cannot be a symlink")
    if work_dir.exists() and (not work_dir.is_dir() or any(work_dir.iterdir())):
        raise ValueError(f"fixture directory must be empty: {work_dir}")
    work_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc).isoformat()
    marker = {"kind": MARKER_KIND, "created_at": created_at, "work_dir": str(work_dir)}
    (work_dir / MARKER_NAME).write_text(json.dumps(marker, indent=2) + "\n")
    started = time.perf_counter()
    real_count = min(256, image_count)
    print(f"fixture: generating {real_count} real 512x512 image assets", flush=True)
    image_paths, real_hashes = _real_assets(work_dir / "assets", real_count, seed)
    query_image = work_dir / "query.png"
    common_query = work_dir / "query-common-grid.png"
    shutil.copyfile(image_paths[1], query_image)
    shutil.copyfile(image_paths[0], common_query)
    db_path = work_dir / "images.sqlite"
    conn = connect(db_path)
    # Bulk-load the disposable fixture before building production summaries.
    # This measures real application reads without timing millions of trigger
    # invocations as part of artificial data generation.
    init_db(conn, defer_search_index=True)
    # Generation-only cache and deferred secondary indexes speed fixture setup;
    # all original indexes are restored before recording fixture completion.
    conn.execute("PRAGMA cache_size = -131072")
    indexes = list(conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"))
    for row in indexes:
        name = str(row["name"]).replace('"', '""')
        conn.execute(f'DROP INDEX "{name}"')
    conn.commit()
    rng = random.Random(seed + 1)
    hash_names = WHOLE_HASH_TYPES + GRID_HASH_TYPES
    timestamp = 1700000000000000000
    tag_count = 0
    common_grid_count = 0
    metadata_compressed_total = 0
    metadata_raw_total = 0
    prompt_chars_total = 0
    distinct_prompt_count = 0
    current_group = -1
    group_tags: list[str] = []
    prompt = ""
    packed = b""
    raw_size = 0
    try:
        for batch_start in range(1, image_count + 1, BATCH_SIZE):
            image_rows, hash_rows, band_rows, prompt_rows, tag_rows = [], [], [], [], []
            for image_id in range(batch_start, min(image_count + 1, batch_start + BATCH_SIZE)):
                group = (image_id - 1) // 10
                if group != current_group:
                    current_group = group
                    group_tags = _tags(group, tags_per_image, seed)
                    prompt = ", ".join(group_tags)
                    packed, raw_size = _metadata(group, prompt, seed, metadata_bytes)
                    distinct_prompt_count += 1
                tags = group_tags
                this_prompt = prompt
                this_packed, this_raw_size = packed, raw_size
                if image_id == 1:
                    tags = [*group_tags[:-1], "rare"]
                    this_prompt = ", ".join(tags)
                    this_packed, this_raw_size = _metadata(group, this_prompt, seed, metadata_bytes)
                    # This one rare tag makes the first image a distinct prompt.
                    distinct_prompt_count += 1
                model = ("nai4", "nai3", "sd")[group % 3]
                if image_id <= real_count:
                    path = image_paths[image_id - 1]
                    hashes = real_hashes[image_id - 1]
                    stat = path.stat()
                    size, mtime = stat.st_size, stat.st_mtime_ns
                else:
                    path = work_dir / "synthetic" / f"folder-{image_id // 1000:06d}" / f"image-{image_id:09d}.png"
                    hashes = {key: rng.randbytes(32 if key in WHOLE_HASH_TYPES else 8) for key in hash_names}
                    hashes["sha256"] = hashlib.sha256(f"{seed}:{image_id}".encode()).digest()
                    if image_id % 100 == 0:
                        hashes["grid0"] = real_hashes[0]["grid0"]
                        common_grid_count += 1
                    size, mtime = 1048576 + image_id % 1000000, timestamp + image_id * 1000000000
                image_rows.append((image_id, str(path), size, mtime, timestamp + image_id * 1000000000,
                                   mtime, 512, 512, "PNG", hashes["sha256"], created_at))
                hash_rows.append((image_id, *(hashes[key] for key in hash_names)))
                for key in hash_names:
                    band_rows.extend((key, index, value, image_id) for index, value in split_bands(hashes[key]))
                prompt_rows.append((image_id, this_prompt, "synthetic", created_at, model, "fixture-model-hash",
                                    model, this_packed, "zlib+utf8", PROMPT_PARSER_VERSION, str(work_dir),
                                    "ok", "unconfigured"))
                tag_rows.extend((image_id, tag, normalize_prompt_tag(tag), position, "prompt") for position, tag in enumerate(tags))
                tag_rows.append((image_id, f"@model:{model}", f"@model:{model}", 1000000000, "model_type"))
                tag_count += len(tags) + 1
                metadata_compressed_total += len(this_packed)
                metadata_raw_total += this_raw_size
                prompt_chars_total += len(this_prompt)
            with conn:
                conn.executemany("INSERT INTO images(id,path,size_bytes,mtime_ns,created_ns,modified_ns,width,height,format,sha256,indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", image_rows)
                conn.executemany("INSERT INTO hashes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", hash_rows)
                conn.executemany("INSERT INTO hash_bands(hash_type,band_index,band_value,image_id) VALUES (?,?,?,?)", band_rows)
                conn.executemany("INSERT INTO image_prompts(image_id,prompt,metadata_source,prompt_indexed_at,model_type,model_hash,model_identity,raw_metadata,raw_metadata_codec,parser_version,classification_root,prompt_status,classification_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", prompt_rows)
                conn.executemany("INSERT INTO prompt_tags(image_id,tag,tag_norm,position,tag_kind) VALUES (?,?,?,?,?)", tag_rows)
            done = min(image_count, batch_start + BATCH_SIZE - 1)
            if done % 10000 == 0 or done == image_count:
                print(f"fixture: {done:,}/{image_count:,} images, {done * 84:,} bands, elapsed {time.perf_counter()-started:.1f}s", flush=True)
        print("fixture: generating sparse match graph", flush=True)
        cluster_count, edge_count = 0, 0
        first_size = min(10000, image_count)
        for start in range(1, image_count + 1, 10000):
            # Batch graph writes independently of cluster boundaries.
            memberships, edges = [], []
            for image_id in range(start, min(image_count + 1, start + 10000)):
                if image_id <= first_size:
                    cluster_id, first = 1, 1
                else:
                    cluster_id = 2 + (image_id - first_size - 1) // 100
                    first = first_size + 1 + (cluster_id - 2) * 100
                memberships.append((cluster_id, image_id, int(image_id == first)))
                cluster_count = max(cluster_count, cluster_id)
                if image_id > first:
                    edges.append((image_id - 1, image_id, 8, 8, 8, 7, 0, 90.0, "strong_duplicate", created_at))
            with conn:
                conn.executemany("INSERT INTO clusters VALUES (?,?,?)", memberships)
                conn.executemany("INSERT INTO matches VALUES (?,?,?,?,?,?,?,?,?,?)", edges)
            edge_count += len(edges)
        print("fixture: restoring all production secondary indexes", flush=True)
        for row in indexes:
            conn.execute(str(row["sql"]))
            conn.commit()
            print(f"fixture: built {row['name']}", flush=True)
        print("fixture: building production search summaries and triggers", flush=True)
        init_db(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise RuntimeError(f"fixture SQLite quick_check failed: {quick_check}")
    finally:
        conn.close()
    manifest = {
        **marker,
        "db_path": str(db_path), "image_count": image_count, "seed": seed,
        "query_image": str(query_image), "common_grid_query_image": str(common_query),
        "image_paths": [str(path) for path in image_paths], "real_image_count": real_count,
        "sqlite_bytes": db_path.stat().st_size,
        "generation_seconds": time.perf_counter() - started,
        "table_counts": {"images": image_count, "hashes": image_count, "hash_bands": image_count * 84,
                         "image_prompts": image_count, "prompt_tags": tag_count,
                         "matches": edge_count, "clusters": image_count, "crop_hashes": 0},
        "distributions": {
            "prompt_tags_per_image": tags_per_image, "model_tags_per_image": 1,
            "common_tag": "common", "common_tag_images": image_count,
            "rare_tag": "rare", "rare_tag_images": 1, "rare_image_id": 1,
            "tail_vocabulary_size": 10000, "images_per_prompt_group": 10,
            "distinct_prompt_count": distinct_prompt_count,
            "mean_prompt_characters": prompt_chars_total / image_count,
            "mean_compressed_metadata_bytes": metadata_compressed_total / image_count,
            "mean_raw_metadata_bytes": metadata_raw_total / image_count,
            "synthetic_common_grid_images": common_grid_count,
            "large_cluster_id": 1, "large_cluster_members": first_size,
            "cluster_count": cluster_count, "remaining_cluster_size": 100,
        },
        "generation_settings": {"metadata_target_bytes": metadata_bytes, "generation_cache_kib": 131072,
                                "secondary_indexes_deferred": True, "analyze_run": False},
        "limitations": [
            "Only the first real_image_count image paths exist; remaining paths are synthetic. Do not interpret this fixture as a real filesystem scan-throughput test.",
            "Synthetic random hashes model band-table lookup cardinality, not a measured natural-image distribution; a controlled 1% cohort shares query-common-grid.png grid0.",
            "Match edges are synthetic chain graphs and do not assert perceptual similarity of underlying image hashes.",
            "Compressed JSON contains deliberately expanded synthetic generation history to model metadata row width, rather than arbitrary padding.",
            "No crop index is generated. Database generation time is excluded from application benchmarks; production indexes are all restored.",
        ],
    }
    (work_dir / "fixture.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"fixture: complete, SQLite {manifest['sqlite_bytes'] / 1024**3:.3f} GiB in {manifest['generation_seconds']:.1f}s", flush=True)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--images", required=True, type=int)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--tags-per-image", type=int, default=24)
    parser.add_argument("--metadata-bytes", type=int, default=2048)
    args = parser.parse_args(argv)
    generate_fixture(args.work_dir, args.images, args.seed, args.tags_per_image, args.metadata_bytes)
    print(args.work_dir.absolute() / "fixture.json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
