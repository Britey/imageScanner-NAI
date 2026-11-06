# imgdupe

Local image and AI prompt-tag search for large personal image collections.

[中文说明](README.zh.md)

`imgdupe` indexes image files with perceptual hashes, then lets you search by uploading or selecting an example image. It can also extract Stable Diffusion and NovelAI prompts into searchable positive tags while keeping bounded source metadata in SQLite.

It does **not** delete files. It stores paths, metadata, and hashes in SQLite.
If you enable missing-record cleanup, only SQLite records are removed; image
files on disk are never deleted.

## Features

- Recursive indexing of one or more folders
- Local SQLite index
- pHash, wHash, dHash, SHA-256, and regional grid hashes
- Local web UI for image search
- Upload or paste an image into the web UI
- NovelAI prompt tag indexing and search
- Prompt-tag autocomplete ranked by database frequency
- Unique-prompt filtering for prompt search results
- Moved-file detection by SHA-256 before optional missing-record cleanup
- Show-in-folder buttons for local result files
- Search controls for strict/balanced/loose matching
- Optional tryhard crop search
- Optional similar-image grouping and static review pages
- Failure reporting for corrupt or unsupported files

## Verification

Install the test extra in your environment, then run the checks explicitly:

```bash
python -m pip install -e '.[test]'
python -m coverage erase
python -m coverage run -m unittest discover -s tests
python -m coverage report
python scripts/check_mutations.py
```

Coverage requires **100% of all `imgdupe` statements and branches**, without
source exclusions. Tests use generated images, temporary SQLite databases,
real background scans and HTTP request handlers, plus controlled failure and
platform API boundaries. Two optional tests skip when the uncommitted reference
image corpus is absent. Native Windows calls are mocked on Linux; coverage is
not a claim of native Windows integration testing. The coverage gate is verified
on Linux with Python 3.13 and 3.14 (SQLite 3.53.1 and 3.46.1).

The mutation runner makes isolated temporary copies and deliberately introduces
realistic semantic defects. Every mutant must trigger an assertion failure;
syntax/import/runtime errors alone do not count as a successful kill. This is a
curated regression check, not an exhaustive mutation score.

The [TLA+ model](formal/README.md) checks unique-tag query safety and termination,
with exhaustive SQL conformance tests and an intentionally failing negative
control. Its README describes the finite bounds, assumptions, and run command.

The GitHub **Manual verification** workflow runs these checks only through
`workflow_dispatch` from the Actions UI or `gh workflow run`; pushing commits or
opening a pull request does not start CI.
See the [recorded local verification results](docs/verification.md).

For reproducible multi-gigabyte SQLite, HTTP, and browser performance testing,
see the [performance testing toolkit](docs/performance-testing.md). It generates
owned synthetic fixtures, retains timings/profiles, and removes disposable
databases after the run. See the [implemented improvements and measured tradeoffs](docs/performance-improvements.md).

## Installation

Clone the repository, create a virtual environment, then install the package:

```powershell
git clone git@github.com:YOUR_GITHUB_USERNAME/imageScanner.git
cd imageScanner
py -3.11 -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -e .
```

After installation, the `imgdupe` command should be available:

```powershell
imgdupe --help
```

If the console command is not on your PATH, use the module form from the activated environment:

```powershell
python -m imgdupe.cli --help
```

Both forms require the package dependencies to be installed. Running `python -m imgdupe.cli` from a fresh clone without creating/installing the environment will not work.

On Windows, `run.bat` performs setup once and then starts directly from the existing
environment on later launches. Run `run.bat setup` after dependency changes. Web UI
settings are stored in the root-level `settings.ini`, including the thumbnail cache
limit and whether advanced crop controls are shown.

Only one web UI instance can run at a time, including copies launched from another
folder. A second launch shows a warning and exits before opening the database.

## Basic Usage

Index a folder recursively:

```powershell
imgdupe scan "E:\Images" --db images.sqlite
```

By default, scanning indexes positive prompt tags from NovelAI metadata and
A1111/Forge-style Stable Diffusion `parameters` metadata.
Images without prompt metadata are recorded as checked and remain searchable by image
hash. For existing databases, backfill prompt metadata without rebuilding hashes:

```powershell
imgdupe scan "E:\Images" --db images.sqlite --workers 16 --reindex-prompts
```

Only positive tags are indexed. A1111 negative prompts, NovelAI Undesired
Content, and negative/zero-weight NovelAI spans are excluded. Emphasis weights,
LoRA directives, generation settings, and common CJK/IME punctuation are cleaned
during indexing. Display tags preserve underscores, while search treats
`sanoba_witch` and `sanoba witch` as equivalent. NovelAI parenthesized tag
qualifiers such as `yuuki (sao)` are preserved; A1111 parentheses remain SD
emphasis syntax.

NovelAI images are detected authoritatively from `Software = NovelAI`; their
`Source` distinguishes NAI3 from NAI4. Other generation metadata is treated as
Stable Diffusion. The full generator identity and generator hash are indexed as
hidden `@model:` metadata. Exactly one category is searchable: the configured
folder label when present, otherwise the detected family. None of these values
are appended to the copyable prompt. Prompt-search results display
the configured folder label when one exists, otherwise the detected category,
followed by the hash; V4.5 is shown as `NAI4 (4.5, <hash>)`.

Folder labels live in the root-level `model_paths.ini`. The web UI asks for an
arbitrary label the first time an unconfigured folder is scanned, and its
"Rescan all configured folders" button skips temporarily offline paths. The
closest configured ancestor wins. For CLI scans, `--model-label noob75` adds
that label to otherwise-unconfigured roots. Legacy `.model_type` files can be
imported by the migration helper, but are no longer consulted during scans.

Rules may also be written manually; `id` is generated when omitted:

```ini
[rule.noob75]
path = E:\ai绘图\noob75
label = noob75
```

Generation metadata with no model name uses the identity
`其他模型 other_model`. Metadata that is present but lacks its expected model
hash is recorded as `missing_model_hash` while the rest of the scan continues.
Images with no generation metadata use the separate `no_metadata` identity.

NovelAI stealth metadata extraction is available, but off by default because it
requires decoding PNG pixels and can be expensive on large collections:

```powershell
imgdupe scan "E:\Images" --db images.sqlite --stealth-prompt
```

Useful prompt-indexing scan options:

- `--reindex-prompts`: re-read prompt metadata for unchanged files.
- `--stealth-prompt`: also try NovelAI alpha-channel stealth metadata for PNGs.
- `--no-prompt-index`: skip prompt extraction and only update the image hash index.

Then start the web UI:

```powershell
imgdupe serve --db images.sqlite --open-browser
```

Open:

```text
http://127.0.0.1:8765
```

The scan command is incremental. Running it again skips unchanged files:

```powershell
imgdupe scan "E:\Images" --db images.sqlite --workers 16
```

On existing databases created before date sorting was added, the same scan
backfills file creation and modification timestamps for unchanged files without
rebuilding image hashes or prompt metadata.

If files have been moved or a folder was renamed, scanning the new location can
claim the old database row before creating a duplicate. When a new path is not
already in the database, `imgdupe` checks missing old paths for a unique
`SHA-256 + file size` match. A match keeps the original image id, hashes,
prompts, matches, and clusters, then updates only the stored path and file
metadata.

To remove database rows for files that still do not exist after move detection,
use:

```powershell
imgdupe scan "E:\Images" --db images.sqlite --workers 16 --delete-missing
```

`--delete-missing` deletes only SQLite records and their related match/cluster
rows. It does not delete image files from disk. Use it only when all indexed
drives and folders are connected.

You can index multiple roots into the same database:

```powershell
imgdupe scan "E:\Images" "F:\Downloads" --db images.sqlite --workers 16
```

## Web UI

The web UI supports:

- selecting an image file
- pasting an image from the clipboard
- English / Chinese language selection
- strict, balanced, and loose matching
- minimum score
- maximum results
- showing or hiding exact file matches
- tryhard crop search
- grouped prompt search where commas/CJK separators mean AND and `|` means OR
- negative prompt tags that exclude images without reindexing
- prompt result sorting by matching-tag count first, then creation date, modified date, or name
- prompt-tag autocomplete with up/down/enter keyboard selection
- optional unique-prompt results, so duplicate prompts show only one image
- optional deletion of database records for files missing after moved-file detection
- show-in-folder buttons that open Explorer and select the image

Tryhard mode can run on a normal index by splitting the query image into many temporary crop and tile variants. It works best with the optional crop index described below.
With that index ready, it can also retrieve images that share only a crop or tile,
including regions at different positions in the two images.

Web forms include a token that changes when the server restarts; refresh an old
page before submitting it after a restart. Requests from other origins or with an
untrusted Host header are rejected. Show-in-folder actions require the page's
POST form, rather than a direct GET link.

Image uploads are limited to 64 MiB per multipart request and other form bodies
to 256 KiB. Headers have a 10-second deadline and bodies a 30-second deadline.
Temporary query uploads are removed after each search, including failed searches.

## Optional Crop Index

The normal index is the recommended default. It is much smaller and works well for near-duplicate search.

If you want stronger cropped-image search, build a larger crop index:

```powershell
imgdupe scan "E:\Images" --db images-crop.sqlite --workers 16 --crop-index
```

This stores many extra crop-region hashes, so the database can become much larger. Use it only when cropped-image recall matters more than index size.

## CLI Search

Search from the command line:

```powershell
imgdupe query "E:\query.jpg" --db images.sqlite --min-score 55
```

Write a simple HTML result page:

```powershell
imgdupe query "E:\query.jpg" --db images.sqlite --html result.html --min-score 55
```

Tryhard cropped search:

```powershell
imgdupe query "E:\cropped.jpg" --db images.sqlite --tryhard --min-score 1
```

Search by prompt or hidden model tags:

```powershell
imgdupe tags "1girl, blue eyes" --db images.sqlite
imgdupe tags "1girl" "blue eyes" --db images.sqlite --mode all
imgdupe tags "cat ears，smile" --db images.sqlite --mode any --limit 200
imgdupe tags "1girl" --not-tags "lowres, blurry" --db images.sqlite
imgdupe tags "1girl" --sort created-desc --db images.sqlite
imgdupe tags "1girl" --model-type nai4 --db images.sqlite
imgdupe tags "@model:4bde2a90" --db images.sqlite
imgdupe tags "cat | dog, solo" --db images.sqlite
```

Tag search uses the same cleaning rules as indexing, so searching for
`sanoba witch` can match prompts containing `sanoba_witch::`.
Negative tags use the existing prompt-tag index and do not require rescanning or
reindexing the database.

In positive searches, `a | b, c` means `(a OR b) AND c`; therefore `a,c`,
`b,c`, and `a,b,c` match, while `a,b` does not. Full-width `｜` is also accepted.

## Similar Image Groups

Build groups of visually similar images:

```powershell
imgdupe cluster --db images.sqlite --min-score 70
```

Generate static review pages:

```powershell
imgdupe review --db images.sqlite --out review
```

Open:

```text
review\index.html
```

## Failure Reports

Show files that failed during indexing:

```powershell
imgdupe failures --db images.sqlite
```

The web UI also has a failures page.

## Notes

- Scanning is recursive.
- Original image files are never modified.
- SQLite remains the source of truth for paths, metadata, hashes, matches, and groups.
- Large indexes are expected. A few GB for hundreds of thousands of images can be normal.
- Scans default to four workers, with bounded task submission so large libraries do not create an unbounded queue. For especially seek-bound mechanical disks, lowering `--workers` to 1 remains available.
