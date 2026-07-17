# Prompt extraction reference manifest

`reference_prompt_tags.json` is the reviewed golden output for the 30 PNG files
in the local `参考图示例` folder. It records each file's SHA-256, metadata-level
model detection, model version, model hash, prompt format, and exact ordered
positive tags.

The entries were checked against the prompt-bearing metadata reported by
ExifTool, rather than inferred from filenames or image content. Negative prompt
content is intentionally absent. The two malformed unbounded-pipe examples and
the two valid NovelAI randomized-prompt examples follow the semantics documented
in `PROMPT_DETECTION_AND_PARSING_PLAN.md`.

`detected_model_type` describes metadata fallback detection before any inherited
`.model_type` override. Consequently, images kept in the `noob11` and `noob75`
reference folders are recorded as `sd`; folder classification is tested
separately.

Run the comparison locally from the project root:

```powershell
python scripts/compare_reference_prompt_tags.py
```

Do not regenerate the manifest blindly after parser changes. Review any changed
tag against the source prompt metadata first, then update only the intentional
golden entries.
