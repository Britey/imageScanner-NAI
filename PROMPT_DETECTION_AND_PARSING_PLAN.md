# Prompt Detection and Parsing Plan

> **2026 path/model amendment:** The `.model_type` design described in older
> sections below is superseded by root-level `model_paths.ini` rules. Labels are
> arbitrary Unicode strings, the closest configured ancestor wins, and folder
> labels are additive hidden metadata rather than parser overrides. The web UI
> requests labels for unconfigured scan roots and can rescan all configured,
> currently available roots. Legacy markers are importable once but are not read
> during normal scans.
>
> Generator metadata is structured as detected family (`sd`, `nai3`, or
> `nai4`), full generator identity, generator hash, and configured path label.
> `Software = NovelAI` is authoritative; its `Source` determines NAI3 versus
> explicit V4/V4.5. SD stores `Model + Model hash`; a missing model name uses
> `其他模型 other_model`. Metadata with a missing hash records the per-image
> `missing_model_hash` error and scanning continues. Completely absent metadata
> uses the distinct `no_metadata` identity.
>
> The full identity and hash are searchable as hidden `@model:` metadata. The
> category has exactly one searchable value: the configured path label when
> present, otherwise the detected family. The detected family remains stored
> internally for diagnostics but must not also match category searches when a
> configured label exists. None of these values are appended to the positive
> prompt. Positive search expressions use comma-like
> separators as AND groups and `|`/`｜` as OR alternatives within a group:
> `a | b, c` means `(a OR b) AND c`. Negative search terms retain their original
> independent-exclusion semantics.

## Archived original design

The remainder records the original parser design and fixtures. Wherever it
mentions fixed `.model_type` categories, hash-less metadata being non-failing,
or fixed model filters, the amendment above is authoritative.

## Purpose

Extend prompt indexing beyond the current NovelAI-only base-prompt extraction so the scanner can:

- classify images as `sd`, `noob`, `nai3`, or `nai4`;
- parse both A1111/Forge-style Stable Diffusion parameters and NovelAI metadata;
- index only searchable positive tags;
- add an available model hash to the positive index as a reserved meta-tag;
- keep negative content out of the positive index, including NovelAI negative-weight spans written inside a positive prompt;
- inherit an explicit model category from a `.model_type` file without requiring every subfolder to be configured separately.

Negative tags will **not** receive rows in `prompt_tags` or a separate negative-tag index. Their only required role is exclusion: the parser must recognize negative content so it is never accidentally inserted into the positive tag index. The original metadata may be retained as raw source data so parsing can be improved later without reopening every image.

## 1. Separate concepts

The implementation must keep these concepts independent:

1. **Model category**: `sd`, `noob`, `nai3`, or `nai4`.
2. **Metadata format**: NovelAI JSON/stealth metadata or A1111-style `parameters` text.
3. **Prompt polarity**: searchable positive content or ignored negative content.

Model names are not authoritative. Blended checkpoints make names unreliable, and `noob` cannot be inferred safely from a model name, image dimensions, prompt vocabulary, or visual appearance.

## 2. Model-category detection

### 2.1 Resolution order

For each image:

1. Search for the closest applicable `.model_type` file.
2. If a valid marker exists, use it as the authoritative category.
3. Otherwise, inspect the embedded `Software` field.
4. If `Software`, after trimming and case folding, equals `NovelAI`, classify the image as NovelAI:
   - use `nai4` when structured `v4_prompt` metadata exists or `Source` identifies V4/V4.5;
   - otherwise use `nai3`.
5. If NovelAI was not detected, default to `sd`.

There is no `unclassified` category.

`noob` is assigned only by an explicit `.model_type` marker.

### 2.2 `.model_type` format

Accepted values are:

```text
sd
noob
nai3
nai4
```

Parsing rules:

- ignore leading and trailing whitespace;
- compare values case-insensitively;
- ignore blank lines;
- ignore lines whose first non-whitespace character is `#`;
- use the first remaining line as the value;
- report an unknown non-empty value as `invalid_model_marker` instead of silently converting it to `sd`.

An invalid marker must not abort the overall scan. It should be recorded in the scan log/summary, and prompt indexing for affected files should be skipped until the typo is fixed. This avoids silently indexing a deliberately categorized folder under the wrong model type.

### 2.3 Inheritance

The closest marker to an image wins. A marker applies to its folder and every descendant unless a deeper marker overrides it.

```text
Images/
  .model_type             # sd
  General/
  Noob/
    .model_type           # noob
    Set A/
    Set B/
  NovelAI/
    .model_type           # nai4
    Old V3/
      .model_type         # nai3
```

Marker lookup stops at the configured scan root. Markers above that root must never affect the scan.

During a scan, cache the effective marker by directory so the application does not walk the same ancestor chain for every image.

### 2.4 Marker changes

Adding, changing, moving, or deleting a `.model_type` file must trigger category and prompt reindexing for affected images even when the image size and modification time are unchanged.

The database should store enough classification provenance to detect this, such as:

- resolved `model_type`;
- marker path, or `NULL` when fallback detection was used;
- a classification/parser version.

## 3. Metadata-format detection

### 3.1 PNG text support

Support all standard PNG textual chunk types used by the supplied images:

- `tEXt`;
- `zTXt`;
- `iTXt`, compressed or uncompressed.

The existing `parse_png_itxt()` implementation must be corrected. Compression flag and method are fixed one-byte fields following the keyword; they cannot be parsed reliably by splitting the entire chunk on NUL bytes.

Pillow metadata may be used as a fallback, but the raw chunk reader should still be corrected and covered by tests.

### 3.2 NovelAI detection

Treat metadata as NovelAI when its `Software` value equals `NovelAI`, case-insensitively after trimming.

Then parse the `Comment` value as JSON. If `Software` says NovelAI but `Comment` is malformed, record `malformed_metadata`; do not reinterpret the image as Stable Diffusion.

NovelAI stealth metadata remains supported as a fallback for PNGs without usable ordinary text metadata.

### 3.3 Stable Diffusion detection

For anything not detected as NovelAI, look for an A1111/Forge-style `parameters` text field.

- If present, parse it using the Stable Diffusion rules below.
- If absent, the image still defaults to category `sd`, but has no indexed prompt.

An explicit model marker controls the category but does not force an incompatible parser. For example, `.model_type = noob` still normally uses the A1111 parameters parser.

## 4. Stored and indexed data

### 4.1 Positive tags only

Only positive tags are inserted into `prompt_tags`.

Do not create database rows for:

- A1111 `Negative prompt:` tags;
- NovelAI Undesired Content (`uc`);
- NAI4 negative base or character captions;
- negative numerical-emphasis spans found inside an otherwise positive NovelAI prompt.

Negative content must still be parsed well enough to exclude it reliably from positive indexing.

### 4.2 Prompt record

Extend `image_prompts` to retain at least:

- the display/searchable positive prompt, or a canonical positive-only representation;
- raw embedded prompt metadata where practical;
- `metadata_source`;
- `prompt_format` (`a1111`, `novelai`, or no metadata);
- `model_type`;
- normalized `model_hash`, when available;
- parser version;
- indexed timestamp;
- extraction error/status.

No negative-tag table or negative-tag index is required.

Store only the relevant A1111 `parameters` text or NovelAI prompt JSON, not unrelated EXIF fields. Encode it as UTF-8 and store it as a zlib-compressed BLOB with a `raw_metadata_codec` value. If compression does not make a small value smaller, `utf8` may be used instead.

Apply a 4 MiB decoded-size safety ceiling to prompt metadata. Metadata over that ceiling is reported as `prompt_metadata_too_large`, is not retained or parsed, and does not abort the scan. This is an internal corruption-safety rule, not a user-facing prompt-length recommendation. The limit prevents one malformed PNG chunk from causing unbounded allocation or database growth.

### 4.3 Tag normalization

For every positive tag:

- trim surrounding whitespace;
- collapse repeated internal whitespace to one space;
- preserve Unicode and the original display spelling;
- preserve underscores, hyphens, apostrophes, colons, and internal periods in the displayed `tag` unless a format-specific rule below removes them;
- create `tag_norm` with Unicode NFC, underscore-to-space conversion, whitespace collapse, and case folding, so `blue_hair` and `blue hair` search as the same tag;
- discard empty tags;
- deduplicate tags case-insensitively within the image;
- preserve the first occurrence's spelling and order.

### 4.4 Model-hash meta-tag

When a generation model hash is available, add it to the same positive `prompt_tags` index using this reserved form:

```text
@model:<normalized-hash>
```

Examples:

```text
@model:6681e8e4b1
@model:c1e1de52
@model:37442fca
```

Rules:

- normalize hexadecimal letters to lowercase;
- preserve the complete hash value supplied by the generator rather than truncating it further;
- insert exactly one model meta-tag per image;
- append it after ordinary positive prompt tags so ordinary prompt ordering is unaffected;
- make the tag searchable through the existing positive prompt-tag search;
- also retain the normalized value in `image_prompts.model_hash` so code does not have to parse it back out of the tag;
- reserve the `@model:` prefix for generated metadata rather than ordinary prompt syntax;
- store generated hash tags with `tag_kind = 'model_hash'`; ordinary prompt tags use `tag_kind = 'prompt'`;
- never assign `tag_kind = 'model_hash'` merely because prompt text begins with `@model:`;
- when a search term begins with `@model:`, restrict matching to `tag_kind = 'model_hash'`, so literal prompt text cannot impersonate extracted model metadata;
- do not create a model meta-tag when no trustworthy model hash is available.

The model-hash meta-tag is independent of `.model_type`. Changing a folder category must not alter the extracted hash.

Short generator hashes are tracking hints, not globally unique identities. Two unrelated checkpoints can theoretically share a truncated hash. The UI and documentation should call this a model-hash tag rather than claiming that it uniquely identifies a checkpoint.

Do **not** confuse the following with the generation model hash:

- the image's own SHA-256 hash;
- A1111 `Lora hashes` entries;
- NovelAI's long `signed_hash` value;
- checkpoint/model display names.

Hash sources are format-specific:

- A1111/Forge: capture `Model hash:` from the generation-settings section before discarding the rest of that section.
- NovelAI: extract the trailing hexadecimal model identifier from `Source`, such as `Stable Diffusion XL C1E1DE52`, `NovelAI Diffusion V4 37442FCA`, or `NovelAI Diffusion V4.5 4BDE2A90`.

The supplied reference set has a usable model identifier for all 26 PNGs: all 18 A1111-style samples have `Model hash:`, and all 8 NovelAI samples have a trailing identifier in `Source`.

### 4.5 Unicode and IME punctuation handling

Prompts may contain Chinese, Japanese, or other non-Latin text, and an IME may insert full-width or CJK punctuation even when the surrounding prompt is otherwise English. The parser must recognize common Unicode punctuation variants without deleting or transliterating the actual prompt text.

Do not apply whole-string NFKC normalization. Compatibility normalization can rewrite legitimate characters and presentation choices. Instead, normalize only known syntax characters while parsing, and preserve the original tag spelling for display.

#### Separator equivalents

Treat these comma-like characters as tag separators:

| Character | Code point | Description |
|---|---:|---|
| `,` | U+002C | Comma |
| `，` | U+FF0C | Full-width comma |
| `、` | U+3001 | Ideographic comma |
| `､` | U+FF64 | Half-width ideographic comma |
| `︐` | U+FE10 | Vertical comma |
| `︑` | U+FE11 | Vertical ideographic comma |
| `﹐` | U+FE50 | Small comma |
| `﹑` | U+FE51 | Small ideographic comma |

Treat these semicolon variants as optional tag separators as well:

| Character | Code point | Description |
|---|---:|---|
| `;` | U+003B | Semicolon |
| `；` | U+FF1B | Full-width semicolon |
| `︔` | U+FE14 | Vertical semicolon |
| `﹔` | U+FE54 | Small semicolon |

Treat Unicode line and paragraph separators as line breaks:

- U+000A LINE FEED;
- U+000D CARRIAGE RETURN;
- U+0085 NEXT LINE;
- U+2028 LINE SEPARATOR;
- U+2029 PARAGRAPH SEPARATOR.

#### Full-stop equivalents

Recognize these as period/full-stop punctuation:

| Character | Code point | Description |
|---|---:|---|
| `.` | U+002E | Full stop |
| `。` | U+3002 | Ideographic full stop |
| `．` | U+FF0E | Full-width full stop |
| `｡` | U+FF61 | Half-width ideographic full stop |
| `︒` | U+FE12 | Vertical ideographic full stop |
| `﹒` | U+FE52 | Small full stop |

CJK/full-width full stops may delimit adjacent prompt tags, particularly when introduced accidentally by an IME:

```text
1girl。青い目。solo
```

becomes:

```text
1girl
青い目
solo
```

ASCII `.` remains subject to the more conservative boundary-period rule so internal values such as `v1.0`, `example.com`, and names containing periods are preserved. A full-width or ideographic full stop surrounded by letters or CJK text is treated as punctuation rather than part of a tag.

#### Syntax punctuation equivalents

When interpreting emphasis and numerical-weight syntax, accept these IME variants as their ASCII equivalents:

- `（` and `）` (U+FF08/U+FF09) as `(` and `)`;
- `［` and `］` (U+FF3B/U+FF3D) as `[` and `]`;
- `｛` and `｝` (U+FF5B/U+FF5D) as `{` and `}`;
- `：` and `﹕` (U+FF1A/U+FE55) as `:`;
- `－` and `−` (U+FF0D/U+2212) as a minus sign only when parsing a numerical weight;
- full-width digits U+FF10 through U+FF19 as ASCII digits only when parsing a numerical weight;
- `．` (U+FF0E) as a decimal point only when it occurs between digits in a numerical weight.

This allows syntax such as the following to be recognized without changing the enclosed CJK tag:

```text
（青い目：0.75）
－１：：帽子：：
```

The first yields the positive tag `青い目`; the second is treated as a negative-weight NovelAI span and `帽子` is excluded from the positive index.

Do not treat CJK quotation/title characters such as `「」`, `『』`, `《》`, or `〈〉` as emphasis wrappers. They may be intentional parts of a title or tag.

#### Text preservation and normalization

- Preserve Chinese, Japanese, Korean, and mixed-script content exactly for display.
- Collapse Unicode whitespace, including U+3000 IDEOGRAPHIC SPACE, to ordinary single spaces for comparison.
- Use Unicode NFC plus case folding for `tag_norm`; do not use NFKC on the whole tag.
- Do not reject a tag merely because it contains no Latin characters.
- Remove candidates made entirely of recognized separator/full-stop punctuation after cleanup.
- Apply Unicode punctuation handling identically to Stable Diffusion and NovelAI prompt text, except where the format-specific period rules say otherwise.

### 4.6 Parser architecture and malformed-input limits

Implement prompt parsing as a single-pass tokenizer plus a small parser/state machine. Do not implement the grammar as an ordered chain of global regular-expression replacements. The state machine must track:

- escapes;
- bracket nesting;
- A1111 edit/alternation constructs;
- NovelAI numerical scopes;
- whether a separator is structural or inside another construct;
- the current positive-index eligibility.

Use regular expressions only for bounded leaf tokens such as a finished number or model hash.

Set a maximum structural nesting depth of 64. If input exceeds it, preserve the remainder as literal text and record a non-fatal `prompt_nesting_too_deep` status. Empty, unclosed, or malformed constructs must never hang, recurse without a bound, or discard unrelated text after the malformed construct.

Parser work must be linear in prompt length.

## 5. Stable Diffusion/A1111 parsing semantics

### 5.1 Divide the parameters field

The field consists of up to three sections:

```text
positive prompt
Negative prompt: negative prompt
Steps: 35, Sampler: ...
```

Rules:

1. Recognize `Negative prompt:` only at the beginning of a line, allowing leading whitespace and case-insensitive matching.
2. Recognize generation settings only from a line beginning with `Steps:` followed by a number and comma.
3. Everything before `Negative prompt:` is positive input.
4. Everything after `Negative prompt:` and before settings is negative input and is ignored for indexing.
5. Everything from the settings line onward is discarded completely.
6. If no negative section exists, all text before settings is positive.
7. If no settings line exists, the current section continues to the end.

Anchoring these markers to line starts prevents an ordinary tag or sentence containing the word `steps` from ending the prompt accidentally.

Before discarding generation settings, capture `Model hash:` for the reserved `@model:` meta-tag. Ignore `Model:`, because blended or renamed model names are not a reliable tracking key.

### 5.2 LoRA removal

Before splitting positive content into tags, remove complete LoRA directives case-insensitively:

```text
<lora:miyako5:0.6>
<lora:745cm:0.66>
<LORA: Q版 : 0.9>
```

The entire directive, including the LoRA name and weight, is discarded. Arbitrary angle-bracket content is not automatically removed unless it matches a recognized LoRA directive.

The `Lora hashes:` settings value is already removed with the generation-settings section.

### 5.3 A1111 emphasis cleanup

Emphasis strength is not stored. Remove unescaped emphasis syntax while keeping its content:

```text
[9-nine-]             -> 9-nine-
[[chibi]]             -> chibi
(tag)                 -> tag
((tag))               -> tag
(izumi tsubasu:0.75)  -> izumi tsubasu
```

Rules:

- strip nested `()` and `[]` wrappers recursively;
- treat a final colon component as a weight only when it parses as a signed or unsigned number;
- discard the numeric weight;
- parse wrapper structure before splitting commas, so `(red hair, blue eyes:1.2)` yields two tags rather than corrupting the group;
- preserve escaped literal delimiters, so `\[example\]` becomes the literal tag `[example]`.

No attempt is required to store, compare, or search by emphasis strength.

### 5.4 Tag separators

After structural cleanup, split positive content on:

- the comma-like Unicode separators defined in Section 4.5;
- the semicolon variants defined in Section 4.5;
- supported line and paragraph separators;
- CJK/full-width full-stop separators as defined in Section 4.5.

Repeated separators must not generate empty tags. Periods, colons, hyphens, and spaces are not general tag separators.

### 5.5 Newline-artifact periods

Remove a single period used as punctuation at a tag boundary:

```text
.chibi       -> chibi
.1girl       -> 1girl
.blue eyes   -> blue eyes
best quality. -> best quality
```

A leading period is removable only when it is the first non-whitespace character after a comma or line boundary. A trailing period is removable only at the end of a tag candidate.

Preserve internal periods:

```text
character.name
v1.0
example.com
```

LoRA removal occurs first, so `<lora:Q版:0.9>.` produces no tag rather than a tag containing only `.`.

### 5.6 A1111 prompt editing and alternation

Square brackets are ambiguous in A1111: they may mean weakening, scheduled prompt editing, or alternating words. Detect the structured forms before applying ordinary weakening cleanup.

#### Scheduled prompt editing

Recognize:

```text
[from:to:when]
[to:when]
[from::when]
```

where the final `when` component is numeric. Index every non-empty textual branch and discard the timing value:

```text
[fantasy:cyberpunk:0.5] -> fantasy, cyberpunk
[rain:0.25]             -> rain
[hat::16]               -> hat
```

Nested edits are parsed recursively subject to the common nesting limit. The database records the union of concepts used during generation; it does not attempt to record which diffusion steps used each concept.

#### Alternating words

Recognize top-level `|` characters inside an A1111 square-bracket construct:

```text
[cow|horse]
[cow|horse|siberian tiger|ox]
```

Index every non-empty alternative. A branch may contain more than one comma-separated tag, and nested constructs are parsed recursively.

If a square-bracket construct matches neither scheduled editing nor alternation, treat it as ordinary weakening and retain its enclosed text.

A1111 alternation must be recognized while the surrounding square brackets are still present. After recognized A1111 constructs and emphasis wrappers have been processed, normalize any remaining unescaped `|` to a comma-equivalent separator. An unbracketed `cow|horse` is therefore indexed as two ordinary positive tags, not retroactively interpreted as A1111 alternating-word syntax.

The keywords `BREAK` and composable-prompt `AND`, when they appear as standalone structural tokens rather than part of a normal tag, act as separators and are not indexed as tags themselves.

### 5.7 Negative A1111 prompt

The negative section must be identified and excluded, but its individual tags do not need to be cleaned, normalized, stored, or indexed.

## 6. NovelAI parsing semantics

### 6.1 Source fields

For NAI3:

```text
prompt -> positive input
uc     -> negative input; do not index
```

For NAI4/4.5, prefer structured fields:

```text
v4_prompt.caption.base_caption
v4_prompt.caption.char_captions[]

v4_negative_prompt.caption.base_caption
v4_negative_prompt.caption.char_captions[]
```

Index the positive base caption and all positive character captions. Parse but do not index the corresponding negative base and character captions.

If structured V4 prompt data is absent or malformed, fall back to top-level `prompt` and `uc`. Do not index both representations when the structured and top-level fields duplicate one another.

### 6.2 Bracket emphasis

NovelAI `{}` strengthening and `[]` weakening affect magnitude only. Strip the unescaped wrappers and retain their contents:

```text
{{best quality}} -> best quality
[[chibi]]        -> chibi
```

These symbols never change prompt polarity.

### 6.3 Numerical emphasis

NovelAI numerical spans have the form:

```text
1.5::rain, night ::
0.5::coat ::
-1::hat ::
-2.5::flat color, simple illustration ::
```

The magnitude is not stored, but the sign controls whether content is eligible for the positive index.

For a **positive** NovelAI source field:

- weight greater than zero: index the contained tags as positive;
- weight equal to zero: do not index the contained tags;
- weight less than zero: treat the contained tags as negative and do not index them.

Examples:

```text
1girl, -1::hat ::, outdoors
```

indexes `1girl` and `outdoors`, but not `hat`.

```text
-2.5::hat, glasses, necktie ::
```

indexes none of the three contained tags.

For an explicitly negative source field such as `uc` or `v4_negative_prompt`, nothing is indexed regardless of its internal weights. There is no need to reverse a negative-weight span in Undesired Content back into the positive index; the entire source field is intentionally excluded from searchable positive tags.

This is a deliberate indexing policy, not a complete simulation of NovelAI's generation mathematics.

### 6.4 Nested numerical spans

Within a positive source field, maintain an effective sign for nested numerical spans:

- any active zero-weight scope makes enclosed content non-indexable;
- an odd number of active negative scopes makes enclosed content negative and non-indexable;
- an even number of active negative scopes restores positive eligibility;
- positive magnitudes do not otherwise matter.

A bare `::` closes the current numerical scope. If a numerical scope is left open, it continues to the end of that source field. A malformed numeric prefix is treated as ordinary prompt text rather than failing the entire image.

### 6.5 NovelAI tag splitting

After resolving emphasis scopes and excluding negative/zero spans, split eligible content using the Unicode/IME separator table in Section 4.5. Apply the common normalization and deduplication rules.

ASCII periods inside a NovelAI caption are preserved unless they are boundary punctuation. CJK/full-width full stops are treated as separators according to Section 4.5.

### 6.6 NovelAI Prompt Randomizer alternation

NovelAI Prompt Randomizer syntax uses double bars around a choice group and single bars between alternatives:

```text
1girl, ||red hair|blue hair|green hair||, rain
```

NovelAI metadata retains the original randomizer expression in `prompt`/`v4_prompt` and, when available, stores the realized choice under `actual_prompts`. The indexing policy is:

1. If `actual_prompts.prompt` is present, prefer its structured positive prompt and index only the realized choice. Read `actual_prompts.prompt.base_caption` and any positive character captions it contains using the same structure rules as `v4_prompt`.
2. `actual_prompts.negative_prompt` is realized negative content and is not indexed.
3. If `actual_prompts.prompt` is absent, do not attempt to evaluate, expand, repair, or infer Prompt Randomizer syntax. Normalize every `|` character in the stored positive prompt to a comma-equivalent separator and index the resulting ordinary positive tags.

The supplied `nai_prompt_randomization` fixtures confirm this layout. Both images contain the same original expression:

```text
1girl, ||black thighhighs|white thighhighs|| solo
```

Their realized fields differ:

```text
actual_prompts.prompt.base_caption -> 1girl, white thighhighs solo
actual_prompts.prompt.base_caption -> 1girl, black thighhighs solo
```

Accordingly, one image indexes `white thighhighs`, the other indexes `black thighhighs`, and neither image indexes the unused alternative.

When the realized field is missing, all pipe runs collapse like repeated commas:

```text
||red hair|blue hair||     -> red hair, blue hair
||red|blue|| hair          -> red, blue, hair
green skirt|white skirt    -> green skirt, white skirt
```

The fallback intentionally does not reconstruct partial alternatives such as `red hair` and `blue hair` in the second example. Without a realized prompt, the application does not know what NovelAI actually used, so it preserves the recoverable text and treats all pipe punctuation uniformly.

The full-width vertical line `｜` (U+FF5C), when encountered in prompt text, follows the same comma-equivalent fallback rule.

### 6.7 Meaning of a top-level single bar

After selecting the best available NovelAI prompt source, handle remaining single bars according to the NovelAI category:

- `nai4`: normalize every `|` to a comma-equivalent tag separator. Do not infer character ownership, section meaning, or a realized random choice from it.
- `nai3`: identify an optional trailing Prompt Mixing weight on each bar-delimited segment, apply its sign-based positive-index eligibility, then treat the bars themselves as comma-equivalent separators.

Structured NAI4 character captions already have explicit fields and are extracted directly under Section 6.1. A raw single bar has no equally reliable provenance: it may be intentional character shorthand, a failed randomizer, or stray punctuation. For indexing purposes, treating it as a comma preserves the positive terms without claiming to know how the model interpreted it.

This rule handles failed attempts to write a V4 randomizer without doubled boundary bars. The supplied `incorrect_examples` fixtures contain no `actual_prompts` field and retain text such as:

```text
1girl, green skirt|white skirt
1girl, white_pantyhose|black_pantyhose
```

These are **not** randomizer groups. Replace the bar with a comma-equivalent separator and index both non-empty positive tags:

```text
green skirt
white skirt

white_pantyhose
black_pantyhose
```

The literal bar is discarded as separator punctuation. Do not claim that either side was the realized random choice, assign either side to a character, or synthesize an `actual_prompts` value. This deliberately indexes the positive text that was actually submitted without guessing how the model interpreted it.

An empty section caused by adjacent or dangling bars contributes no tag. Malformed bars must not consume or suppress valid comma-separated tags before or after them.

For NAI3 Prompt Mixing, a segment may end in a mixing weight:

```text
cat:1|happy:-0.2|cute:-0.3
Prompt1|Prompt2:0.3
```

Rules:

- no explicit trailing mix weight means positive weight `1`;
- a positive mix weight allows that segment's tags into the positive index;
- a zero or negative mix weight excludes that segment's tags from the positive index;
- remove the trailing mix weight rather than indexing it as part of a tag;
- an internal colon that is not the segment's final numeric suffix remains literal;
- numerical `value::...::` emphasis is parsed before looking for a single-colon trailing mix weight.

As with negative numerical emphasis, this is an indexing policy: magnitude is discarded, while sign determines positive-index eligibility.

## 7. Database and compatibility plan

### 7.1 Preserve existing databases

Existing databases remain valid. Apply additive schema migrations rather than replacing the database.

The current `prompt_tags` table remains the positive-tag index. Existing prompt search behavior remains positive-only.

Likely additions to `image_prompts` are:

- `model_type TEXT`;
- `model_hash TEXT`;
- `prompt_format TEXT`;
- `raw_metadata BLOB`;
- `raw_metadata_codec TEXT`;
- `parser_version INTEGER`;
- `classification_root TEXT`;
- `model_marker_path TEXT`;
- prompt/classification status fields.

Add `tag_kind TEXT NOT NULL DEFAULT 'prompt'` to `prompt_tags`. Existing rows migrate as `prompt`. Generated model-hash rows use `model_hash`.

Add these indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_image_prompts_model_type_image
ON image_prompts(model_type, image_id);

CREATE INDEX IF NOT EXISTS idx_prompt_tags_kind_norm_image
ON prompt_tags(tag_kind, tag_norm, image_id);
```

Keep the existing tag index until query plans confirm the new composite index fully replaces it.

Schema migration must be idempotent and transactional. Use SQLite schema introspection before each `ALTER TABLE`, then update the schema/parser metadata version only after the entire migration commits.

Do not add a negative-tag index.

### 7.2 Reindex requirements

The first rollout requires a prompt-only reindex of existing images because:

- current extraction ignores A1111 plaintext `parameters`;
- current extraction indexes only one NovelAI prompt field;
- current extraction omits NAI4 character captions;
- current cleanup incorrectly leaves negative numerical NovelAI spans eligible for positive indexing;
- current PNG `iTXt` parsing misses some valid sample metadata.

The reindex must not recompute hashes, crops, duplicate matches, or clusters.

After raw prompt metadata has been retained, later parser-version changes should rebuild positive tags from the database wherever possible instead of reopening original image files.

### 7.3 Batched and resumable prompt reindex

Process the initial prompt-only reindex in ascending `images.id` order in transactions of 500 images. The batch size is an internal implementation constant and may be tuned from measurements; it is not a user-facing limit.

Store reindex state in `app_metadata`, including:

```text
prompt_reindex_target_version
prompt_reindex_last_committed_image_id
prompt_reindex_status
```

For each batch:

1. Begin a transaction.
2. Resolve model classification for each image, even if the image file itself is unchanged.
3. Read source metadata only when the row lacks reusable raw metadata for the target parser version.
4. Delete and replace that image's positive prompt-tag rows inside the same transaction.
5. Write the row's new parser version only after its replacement rows are ready.
6. Update the committed cursor at the end of the batch.
7. Commit.

If the application exits or fails mid-batch, SQLite rolls back that batch and the next launch resumes after `prompt_reindex_last_committed_image_id`. Already committed batches are not repeated. Cancellation is checked between images and honored at a batch boundary.

Prompt-only reindexing must not modify `hashes`, `hash_bands`, `crop_hashes`, `matches`, or `clusters`.

### 7.4 Scan-root provenance

Store the canonical active scan root used for marker inheritance. If an image is later scanned through a different or more specific root, re-resolve `.model_type` and perform a prompt/classification-only update.

If one invocation contains overlapping roots, assign each image to the most specific configured root containing it. Resolve an identical path only once per invocation. This makes marker inheritance deterministic.

### 7.5 Images without prompts

An image may have a valid model category but no prompt metadata. Store the category/status without inserting prompt tags.

Recommended internal statuses are:

```text
ok
no_prompt_metadata
malformed_metadata
unsupported_metadata
invalid_model_marker
prompt_metadata_too_large
prompt_nesting_too_deep
```

Failures are per-image and must not abort the overall scan. They may appear in logs or a scan summary without generating repetitive warnings in the normal browsing UI.

## 8. Implementation stages

### Stage 1: Metadata correctness

1. Fix uncompressed and compressed PNG `iTXt` parsing.
2. Read `Software`, `Source`, `Comment`, and `parameters` without assuming every value is JSON.
3. Extract A1111 `Model hash:` and NovelAI's trailing `Source` identifier.
4. Separate metadata-format detection from model-category detection.
5. Add fixtures for every supplied metadata layout.

### Stage 2: Model classification

1. Implement `.model_type` parsing and closest-ancestor inheritance.
2. Stop marker lookup at the scan root.
3. Cache directory resolutions during each scan.
4. Add NovelAI fallback detection using `Software = NovelAI`.
5. Default every non-NovelAI unmarked image to `sd`.
6. Detect marker changes independently of image modification times.

### Stage 3: Prompt parsers

1. Add an A1111 section parser.
2. Remove LoRA directives.
3. Parse A1111 scheduled edits and alternating-word branches before ordinary weakening cleanup.
4. Strip A1111 emphasis wrappers and weights.
5. Parse NAI3 positive prompt and exclude `uc`.
6. Parse NAI4 base and character captions.
7. Add a scope-aware NovelAI numerical-emphasis parser.
8. Prefer NovelAI `actual_prompts.prompt`; when it is absent, normalize all pipe characters to comma-equivalent separators rather than evaluating randomizer syntax.
9. Interpret NAI3 trailing Prompt Mixing weights before normalizing their pipe separators.
10. Exclude negative and zero-weight NovelAI spans/mixes from positive tags.
11. Add targeted Unicode/IME punctuation handling without rewriting CJK text.
12. Append a normalized `@model:<hash>` meta-tag when a model hash is available.
13. Preserve bounded, compressed raw prompt metadata and parser version.

### Stage 4: Schema migration and scan integration

1. Add classification/format/raw metadata columns additively and transactionally.
2. Add `tag_kind` and category/query indexes.
3. Continue using `prompt_tags` exclusively for positive searchable tags.
4. Integrate batched, checkpointed prompt-only reindexing into scan decisions.
5. Reindex when parser version, governing scan root, or effective marker changes.
6. Do not trigger image hashing or duplicate recomputation for prompt-only changes.

### Stage 5: UI integration

1. Add model-category filters: `all`, `sd`, `noob`, `nai3`, and `nai4`.
2. Keep the existing positive prompt search semantics.
3. Do not add negative-tag search controls.
4. Show the detected/assigned model category where useful.
5. Render `tag_kind = 'model_hash'` distinctly and make it clickable/searchable as `@model:<hash>`.
6. Ensure `@model:` searches match only generated model-hash rows.
7. Keep parser failures in logs or compact scan summaries rather than repetitive UI warnings.

## 9. Required tests

### Model detection

- closest `.model_type` overrides an ancestor;
- lookup stops at scan root;
- marker values are case-insensitive;
- invalid marker is reported;
- marker addition/change/deletion triggers prompt-only reindex;
- `Software = NovelAI` selects NovelAI fallback;
- V4 fields select `nai4`;
- other NovelAI metadata selects `nai3`;
- everything else defaults to `sd`;
- `noob` is never guessed without a marker.

### PNG metadata

- `tEXt`, `zTXt`, uncompressed `iTXt`, and compressed `iTXt`;
- malformed chunks do not abort scanning;
- A1111 plaintext `parameters` is not rejected for failing JSON parsing;
- stealth NovelAI fallback remains functional.

### Model-hash meta-tags

- all 26 supplied reference PNGs produce one `@model:` tag;
- A1111 `Model hash: 6681e8e4b1` produces `@model:6681e8e4b1`;
- NovelAI `Source: Stable Diffusion XL C1E1DE52` produces `@model:c1e1de52`;
- NovelAI V4/V4.5 `Source` identifiers are extracted the same way;
- hexadecimal case is normalized;
- the full supplied model hash is preserved;
- image SHA-256, LoRA hashes, `signed_hash`, and model names never produce `@model:` tags;
- a missing or malformed model hash produces no meta-tag and does not fail prompt parsing;
- changing `.model_type` leaves the model meta-tag unchanged.

### Stable Diffusion parsing

- positive, negative, and settings sections are separated;
- all settings after `Steps:` are discarded;
- LoRA directives disappear entirely;
- `[9-nine-]` becomes `9-nine-`;
- `(izumi tsubasu:0.75)` becomes `izumi tsubasu`;
- nested wrappers are removed;
- escaped brackets remain literal;
- grouped comma tags survive parsing;
- `[fantasy:cyberpunk:0.5]` indexes `fantasy` and `cyberpunk` but not `0.5`;
- `[rain:0.25]` and `[hat::16]` retain their non-empty text branches;
- `[cow|horse|ox]` indexes all three alternatives;
- unbracketed A1111 `cow|horse` is normalized like `cow, horse` after structured alternation parsing;
- nested scheduled edits and alternations obey the depth limit;
- standalone `BREAK` and structural `AND` do not become tags;
- boundary periods are removed while internal periods remain;
- negative prompt tags never enter `prompt_tags`.

### NovelAI parsing

- NAI3 `prompt` is indexed and `uc` is excluded;
- NAI4 base and every character caption are indexed;
- NAI4 negative captions are excluded;
- positive numerical weights retain tags;
- weakening weights between zero and one retain tags;
- zero-weight spans are excluded;
- negative-weight spans inside positive prompts are excluded;
- multiple tags inside one negative span are all excluded;
- nested negative scopes apply effective-sign semantics;
- an unclosed negative scope continues to end of field;
- malformed emphasis does not abort the image;
- top-level prompt fallback does not duplicate structured V4 prompt content.

### NovelAI randomizer and bar syntax

- `actual_prompts.prompt.base_caption` takes precedence over the original randomizer expression;
- structured positive character captions under `actual_prompts.prompt` are included when present;
- `actual_prompts.negative_prompt` is never indexed;
- the supplied white-thighhighs image indexes `white thighhighs` but not `black thighhighs`;
- the supplied black-thighhighs image indexes `black thighhighs` but not `white thighhighs`;
- without `actual_prompts`, `||red hair|blue hair||` is normalized like `red hair, blue hair`;
- without `actual_prompts`, `||red|blue|| hair` is normalized like `red, blue, hair` and is not reconstructed into color-hair combinations;
- every run of pipes without a realized prompt behaves like repeated commas and contributes no empty tags;
- the supplied malformed `green skirt|white skirt` V4 prompt is normalized like `green skirt, white skirt` and indexes both tags;
- the supplied malformed `white_pantyhose|black_pantyhose` V4 prompt indexes both normalized tags;
- U+FF5C FULLWIDTH VERTICAL LINE follows the same comma-equivalent fallback;
- no unresolved pipe creates inferred character ownership or realized-choice metadata;
- unmatched/dangling bars terminate safely and do not suppress neighboring tags;
- NAI3 `cat:1|happy:-0.2|cute:-0.3` indexes `cat` but excludes `happy` and `cute`;
- NAI3 positive mix weights retain the segment and do not become part of its final tag;
- internal literal colons are preserved;
- numerical `::` scopes are not mistaken for trailing Prompt Mixing weights.

### Unicode and IME input

- U+FF0C FULLWIDTH COMMA and U+3001 IDEOGRAPHIC COMMA split tags;
- half-width, small, and vertical comma variants split tags;
- full-width/vertical semicolons split tags;
- U+3002 IDEOGRAPHIC FULL STOP and its full-width/half-width variants split adjacent tags;
- Unicode line and paragraph separators split tags;
- U+3000 IDEOGRAPHIC SPACE is normalized as whitespace;
- U+FF5C FULLWIDTH VERTICAL LINE is normalized as a pipe and follows the format-specific comma fallback;
- full-width brackets are recognized as emphasis syntax;
- full-width colons, digits, decimal point, and minus signs are recognized inside numerical weights;
- `－１：：帽子：：` excludes `帽子` from the positive index;
- random Chinese, Japanese, Korean, and mixed-script tags remain searchable;
- `「title」`, `『title』`, `《title》`, and `〈title〉` are preserved rather than stripped as emphasis;
- internal ASCII periods in `v1.0`, `example.com`, and similar tags are preserved;
- punctuation-only candidates are discarded;
- whole-tag NFKC normalization is never applied.
- `blue_hair` preserves its display spelling but shares `tag_norm` with `blue hair`.

### Database compatibility

- an old database opens and migrates successfully;
- rerunning schema migration is a no-op rather than an error;
- positive prompt searches return the same existing positive tags after migration;
- no negative-tag table or index is created;
- existing prompt rows receive `tag_kind = 'prompt'`;
- only extracted model metadata receives `tag_kind = 'model_hash'`;
- `image_prompts(model_type, image_id)` is used by category-filter query plans;
- prompt-only reindex does not rewrite image hashes or duplicate results;
- parser-version changes reindex prompt data only;
- simulated interruption rolls back only the active 500-image batch and resumes after the last committed image ID;
- a row never advertises the target parser version while retaining tags produced by an older parser;
- changing or overlapping scan roots resolves markers deterministically;
- compressed raw metadata round-trips exactly;
- metadata over 4 MiB is rejected without excessive allocation or scan failure;
- nesting beyond 64 levels terminates with a non-fatal status.

## 10. Acceptance criteria

The feature is complete when:

1. Every supplied reference image receives the expected `sd`, `noob`, `nai3`, or `nai4` category.
2. Folder markers apply recursively and can be overridden in descendants.
3. All supplied A1111 parameters fields are detected, including the valid `iTXt` samples currently missed.
4. Stable Diffusion LoRA directives, emphasis wrappers, weights, negative prompts, and generation settings never become searchable positive tags.
5. NAI4 character captions contribute searchable positive tags.
6. NovelAI negative numerical-emphasis spans inside positive prompts do not contribute searchable positive tags.
7. A1111 editing/alternation and NovelAI randomizer/mixing syntax produce useful positive tags without indexing timing or negative-weight branches; ambiguous NAI4 single bars are treated as commas and never guessed into a successful random choice.
8. Every supplied reference image has one searchable `@model:<hash>` meta-tag derived from its generation metadata.
9. Common IME punctuation variants split and normalize prompt syntax without corrupting Chinese, Japanese, Korean, or mixed-script tags.
10. No individual negative tags are stored or indexed.
11. Existing databases migrate without losing image hashes, matches, clusters, or existing application settings.
12. Reclassification and parser upgrades can perform resumable prompt-only reindexing without a full image-analysis rescan.
13. Prompt parsing remains bounded and non-fatal for malformed, deeply nested, or oversized metadata.

## 11. Syntax references

- [NovelAI Prompt Randomizer](https://docs.novelai.net/en/image/promptrandomizer/): `||...|...||` option syntax, partial/multi-tag options, original-versus-actual prompt behavior, and interaction with top-level character separators.
- [NovelAI Advanced Prompt Mixing](https://docs.novelai.net/en/image/promptmixing/): V3-or-lower top-level `|` mixing and signed trailing segment weights.
- [NovelAI Multi-Character Prompting](https://docs.novelai.net/en/image/multiplecharacters/): V4 top-level `|` base/character separation and structured character prompt fields.
- [NovelAI Strengthening and Weakening](https://docs.novelai.net/en/image/strengthening-weakening/): bracket emphasis, numerical `::` scopes, and negative numerical emphasis.
- [AUTOMATIC1111 Features](https://github.com/AUTOMATIC1111/stable-diffusion-webui/wiki/Features): `[from:to:when]` prompt editing and `[a|b]` alternating-word syntax.
