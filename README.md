# imgdupe

Local image and NovelAI prompt-tag search for large personal image collections.

[中文说明](README.zh.md)

`imgdupe` indexes image files with perceptual hashes, then lets you search by uploading or selecting an example image. It can also extract NovelAI prompts into searchable tags while keeping the original prompt text in SQLite.

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
imgdupe scan "E:\Images" --db images.sqlite --workers 16
```

By default, scanning also attempts to index NovelAI prompt tags from image metadata.
Images without prompt metadata are recorded as checked and remain searchable by image
hash. For existing databases, backfill prompt metadata without rebuilding hashes:

```powershell
imgdupe scan "E:\Images" --db images.sqlite --workers 16 --reindex-prompts
```

Prompt text is stored as-is, but searchable tags are cleaned during indexing:
NovelAI weight markers such as `1.2::tag::`, bare `::`, `{}`, `[]`, and
underscores are ignored for search. For example, `sanoba_witch::` is indexed as
`sanoba witch`.

NovelAI stealth metadata extraction is available, but off by default because it
requires decoding PNG pixels and can be expensive on large collections:

```powershell
imgdupe scan "E:\Images" --db images.sqlite --workers 16 --stealth-prompt
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
- prompt tag search using commas, Chinese commas, or newlines
- negative prompt tags that exclude images without reindexing
- prompt result sorting by matching-tag count first, then creation date, modified date, or name
- prompt-tag autocomplete with up/down/enter keyboard selection
- optional unique-prompt results, so duplicate prompts show only one image
- optional deletion of database records for files missing after moved-file detection
- show-in-folder buttons that open Explorer and select the image

Tryhard mode can run on a normal index by splitting the query image into many temporary crop and tile variants. It works best with the optional crop index described below.

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

Search by NovelAI prompt tags:

```powershell
imgdupe tags "1girl, blue eyes" --db images.sqlite
imgdupe tags "1girl" "blue eyes" --db images.sqlite --mode all
imgdupe tags "cat ears，smile" --db images.sqlite --mode any --limit 200
imgdupe tags "1girl" --not-tags "lowres, blurry" --db images.sqlite
imgdupe tags "1girl" --sort created-desc --db images.sqlite
```

Tag search uses the same cleaning rules as indexing, so searching for
`sanoba witch` can match prompts containing `sanoba_witch::`.
Negative tags use the existing prompt-tag index and do not require rescanning or
reindexing the database.

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
- For best scan speed, tune `--workers`; too many workers can hurt performance on slower disks.
