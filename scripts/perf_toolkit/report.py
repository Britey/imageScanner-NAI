"""Portable reports from retained benchmark JSON; no extra plotting dependency."""
from __future__ import annotations

import html
import json
from pathlib import Path


def write_reports(summary: dict, output: Path) -> None:
    rows = []
    web_rows = []
    lines = ["# Performance benchmark results", "", f"Application commit: `{summary['environment']['commit']}`", "",
             f"CPU: {summary['environment']['cpu']}", "", "## Method", ""]
    lines.extend(f"- {note}" for note in summary["methodology"])
    lines.extend(["", "## Backend operation timings", "",
                  f"Planned samples per case: {summary['configuration']['repeats']}. A failure stops remaining repeats. "
                  "Medians are from successful samples only; missing results are not zero.", "",
                  "| Images | DB GiB | Operation | Samples | Median seconds | Min–max seconds | Peak worker RSS MiB |",
                  "|---:|---:|---|---:|---:|---:|---:|"])
    for run in summary["runs"]:
        count = run["fixture"]["image_count"]
        gib = run["fixture"]["sqlite_bytes"] / 1024**3
        for case, result in run["backend"].items():
            successful = result["successful_samples"]
            if successful:
                med = result["median_seconds"]
                lines.append(f"| {count:,} | {gib:.3f} | {case} | {successful}/{result['sample_count']} | {med:.6f} | "
                             f"{result['min_seconds']:.6f}–{result['max_seconds']:.6f} | {result['max_peak_rss_mib']:.1f} |")
                rows.append({"images": count, "case": case, "seconds": med, "rss": result["max_peak_rss_mib"],
                             "samples": successful, "attempted": result["sample_count"],
                             "status": "ok" if successful == result["sample_count"] else "partial failure",
                             "min": result["min_seconds"], "max": result["max_seconds"]})
            else:
                status = ", ".join(s.get("status", "unknown") for s in result["samples"])
                lines.append(f"| {count:,} | {gib:.3f} | {case} | 0/{result['sample_count']} | {status} | — | — |")
                rows.append({"images": count, "case": case, "seconds": None, "rss": None,
                             "samples": 0, "attempted": result["sample_count"], "status": status, "min": None, "max": None})
    lines.extend(["", "## Web UI", "", "Actual loopback HTTP. Asset batches contain up to 100 thumbnails at concurrency six; "
                  "their batch duration is separate from individual-request medians. Browser measurements, when run, are in `browser.json`.", "",
                  "| Images | HTTP case | Successful / attempted | Median seconds | Asset/burst total seconds |",
                  "|---:|---|---:|---:|---:|"])
    hol_notes = []
    for run in summary["runs"]:
        count = run["fixture"]["image_count"]
        for case, value in run.get("web", {}).get("cases", {}).items():
            stats = value["summary"]
            median = stats.get("median_seconds")
            batch = value.get("page_asset_seconds", value.get("batch_seconds"))
            med_text = f"{median:.6f}" if median is not None else "failed"
            batch_text = f"{batch:.6f}" if batch is not None else "—"
            lines.append(f"| {count:,} | {case} | {stats['successful_samples']}/{stats['samples']} | {med_text} | {batch_text} |")
            if median is not None and case != "head_of_line":
                web_rows.append({"images": count, "case": case, "seconds": median, "rss": 0,
                                 "samples": stats["successful_samples"], "attempted": stats["samples"],
                                 "status": "ok" if stats["errors"] == 0 else "partial failure",
                                 "min": stats["min_seconds"], "max": stats["max_seconds"]})
            if batch is not None:
                web_rows.append({"images": count, "case": case + "_batch", "seconds": batch, "rss": 0,
                                 "samples": 1, "attempted": 1, "status": "ok", "min": batch, "max": batch})
            if case == "head_of_line":
                for key in ("isolated_fast", "contended_fast"):
                    item = value[key]
                    duration = item["duration_seconds"]
                    web_rows.append({"images": count, "case": key, "seconds": duration, "rss": 0,
                                     "samples": 1, "attempted": 1, "status": str(item.get("status")), "min": duration, "max": duration})
        hol = run.get("web", {}).get("cases", {}).get("head_of_line")
        if hol:
            hol_notes.extend(["", f"At {count:,} images, the cached-thumbnail request took "
                          f"{hol['isolated_fast']['duration_seconds']:.6f}s in isolation and "
                          f"{hol['contended_fast']['duration_seconds']:.6f}s when sent during a common-tag search. "
                          f"Observed request overlap: {hol['request_intervals_overlap']}.", ""])
    lines.extend(hol_notes)
    lines.extend(["", "## Evidence", "", "Actual loopback HTTP raw samples are retained in `n*/web.json`.", "",
                  "- `summary.json`: environment, fixture distributions, all backend samples, and HTTP results.",
                  "- `n*/diagnostics/`: separate cProfile, bounded SQL traces, and EXPLAIN QUERY PLAN evidence.",
                  "- `n*/fixture-manifest.json`: row counts, byte sizes, asset coverage, and limitations.",
                  "- `n*/*.log`: generation and worker output.", "", "## Cleanup", "",
                  f"Generated run workspace removed: **{summary['fixtures_removed']}**.", ""])
    if summary.get("error"):
        lines.extend(["Run error: " + summary["error"], ""])
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    data = json.dumps({"backend": rows, "web": web_rows}).replace("<", "\\u003c")
    evidence = html.escape(summary["environment"]["commit"][:12])
    document = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ImageScanner performance measurements</title><style>
:root{color-scheme:light dark;--background:#fff;--foreground:#18212f;--muted-foreground:#64748b;--border:#cbd5e1;--chart-1:#277da8;--chart-2:#d27520;--font-sans:system-ui,sans-serif;--radius:8px}
@media(prefers-color-scheme:dark){:root{--foreground:#e5e7eb;--muted-foreground:#94a3b8;--border:#475569}}
body{font:14px/1.5 var(--font-sans);color:var(--foreground);margin:0}main{max-width:1000px;margin:auto}h1{font-size:22px}p{color:var(--muted-foreground)}
.controls{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}select{font:inherit;padding:5px}svg{display:block;width:100%;height:490px}text{fill:var(--foreground);font-family:var(--font-sans)}
table{border-collapse:collapse;width:100%;font-size:12px}td,th{padding:7px;border-bottom:1px solid var(--border);text-align:right}td:first-child,th:first-child{text-align:left}
.legend span{display:inline-flex;align-items:center;margin-right:16px}.legend i{width:12px;height:12px;display:inline-block;margin-right:6px}.muted{color:var(--muted-foreground)}
</style><main><h1>Measured backend performance</h1><p>Commit COMMIT. Actual operation medians; fresh worker processes, OS cache not flushed. SQL/profile runs excluded.</p>
<div class="controls"><label>Measurements <select id="dataset"><option value="backend">Backend</option><option value="web">Actual HTTP / assets</option></select></label><label>Metric <select id="metric"><option value="seconds">Wall time (seconds)</option><option value="rss">Peak worker RSS (MiB, backend only)</option></select></label>
<label>Scale <select id="scale"><option value="log">Logarithmic</option><option value="linear">Linear</option></select></label></div>
<div class="legend" id="legend"></div><svg id="chart" role="img" aria-label="Backend latency comparison"></svg><p class="muted">Hover a bar for its value. RSS includes interpreter/import memory. Latency bars show median, not tail percentiles.</p>
<table><thead><tr><th>Operation</th><th>Images</th><th>Median seconds</th><th>Min–max</th><th>Peak RSS MiB</th><th>Successful / attempted</th><th>Status</th></tr></thead><tbody id="rows"></tbody></table></main>
<script>const datasets=DATA;
function draw(){const all=datasets[document.querySelector('#dataset').value];const data=all.filter(x=>x.seconds!==null);const sizes=[...new Set(all.map(x=>x.images))];const cases=[...new Set(data.map(x=>x.case))];
document.querySelector('#legend').innerHTML=sizes.map((n,i)=>`<span><i style="background:var(--chart-${i%6+1})"></i>${n.toLocaleString()} images</span>`).join('');
document.querySelector('#rows').innerHTML=all.map(d=>`<tr><td>${d.case}</td><td>${d.images.toLocaleString()}</td><td>${d.seconds===null?'—':d.seconds.toFixed(4)}</td><td>${d.min===null?'—':d.min.toFixed(4)+'–'+d.max.toFixed(4)}</td><td>${d.rss===null?'—':d.rss.toFixed(1)}</td><td>${d.samples}/${d.attempted}</td><td>${d.status}</td></tr>`).join('');
const metric=document.querySelector('#metric').value, log=document.querySelector('#scale').value==='log';const svg=document.querySelector('#chart');const w=950,h=Math.max(360,cases.length*29+45),left=215,right=30;
if(!data.length){svg.innerHTML='<text x="20" y="40">No successful measurements; inspect status rows and logs.</text>';return;}
svg.style.height=h+'px';svg.setAttribute('viewBox',`0 0 ${w} ${h}`);const vals=data.map(d=>Math.max(d[metric],1e-6));const max=Math.max(...vals,1e-6),min=log?Math.pow(10,Math.floor(Math.log10(Math.min(...vals)))):0;
const base=log?Math.log10(min):0,top=log?Math.ceil(Math.log10(max*1.01)):max*1.05;const x=v=>left+(log?Math.log10(Math.max(v,min))-base:v)/(top-base)*(w-left-right);
let s='';for(let i=0;i<=5;i++){const val=log?10**(base+(top-base)*i/5):top*i/5;const xx=x(val);s+=`<line x1="${xx}" y1="20" x2="${xx}" y2="${h-25}" stroke="var(--border)"/><text x="${xx}" y="${h-5}" text-anchor="middle" font-size="10">${val.toPrecision(2)}</text>`;}
cases.forEach((c,j)=>{const y=25+j*29;s+=`<text x="${left-9}" y="${y+8}" text-anchor="end" font-size="11">${c}</text>`;data.filter(d=>d.case===c).forEach(d=>{const i=sizes.indexOf(d.images),bh=Math.min(10,22/sizes.length);s+=`<rect x="${left}" y="${y+i*bh}" width="${Math.max(2,x(d[metric])-left)}" height="${bh-1}" fill="var(--chart-${i%6+1})"><title>${c}, ${d.images} images: ${d[metric].toFixed(6)} ${metric==='seconds'?'seconds':'MiB'}</title></rect>`;});});svg.innerHTML=s;}
document.querySelectorAll('select').forEach(x=>x.addEventListener('change',draw));draw();</script></html>'''.replace("DATA", data).replace("COMMIT", evidence)
    (output / "report.html").write_text(document, encoding="utf-8")
