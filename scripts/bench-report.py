#!/usr/bin/env python3
"""
bench-report.py — regenerate benchmarks/README.md from jsonl results.

    make bench-report

Reads every benchmarks/results/*.jsonl file, keeps the newest row per (model, scenario),
and writes markdown tables sorted with the latest runs first.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(REPO_ROOT, "benchmarks", "results")
README_PATH = os.path.join(REPO_ROOT, "benchmarks", "README.md")

def parse_ts(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return datetime.min


def load_rows() -> list[dict]:
    paths = sorted(glob.glob(os.path.join(RESULTS_DIR, "*.jsonl")))
    rows: list[dict] = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    print(f"warning: {path}:{line_no}: {exc}", file=sys.stderr)
    return rows


def latest_by_model_scenario(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """model -> scenario -> newest row."""
    best: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        model = row.get("model") or "?"
        scenario = row.get("scenario") or "?"
        prev = best[model].get(scenario)
        if prev is None or parse_ts(row.get("timestamp", "")) >= parse_ts(prev.get("timestamp", "")):
            best[model][scenario] = row
    return best


def fmt_num(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        if abs(v) >= 100:
            return f"{v:.0f}"
        return f"{v:.2f}"
    return str(v)


def single_stream_table(rows: dict[str, dict]) -> str:
    lines = [
        "### Single-stream (latest per model)",
        "",
        "| Model | When | Prompt k | TTFT (s) | Prefill tok/s | Decode tok/s |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    items = []
    for model, sc in rows.items():
        row = sc.get("single-stream")
        if not row:
            continue
        m = row.get("metrics") or {}
        items.append((parse_ts(row.get("timestamp", "")), model, row, m))
    items.sort(key=lambda x: x[0], reverse=True)
    if not items:
        lines.append("| _no data_ | | | | | |")
    for _, model, row, m in items:
        when = (row.get("timestamp") or "")[:19].replace("T", " ")
        lines.append(
            f"| `{model}` | {when} | {fmt_num(m.get('prompt_size_k'))} | "
            f"{fmt_num(m.get('ttft_s'))} | {fmt_num(m.get('prefill_tps'))} | "
            f"{fmt_num(m.get('decode_tps'))} |"
        )
    lines.append("")
    return "\n".join(lines)


def concurrency_table(rows: dict[str, dict]) -> str:
    lines = [
        "### Concurrency (latest per model)",
        "",
        "| Model | When | N | Aggregate tok/s | Per-stream avg | TTFT p50 (s) | TTFT p95 (s) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    items = []
    for model, sc in rows.items():
        for scenario, row in sc.items():
            if scenario != "concurrency":
                continue
            m = row.get("metrics") or {}
            items.append((parse_ts(row.get("timestamp", "")), model, row, m))
    items.sort(key=lambda x: x[0], reverse=True)
    if not items:
        lines.append("| _no data_ | | | | | | |")
    for _, model, row, m in items:
        when = (row.get("timestamp") or "")[:19].replace("T", " ")
        lines.append(
            f"| `{model}` | {when} | {fmt_num(m.get('concurrency'))} | "
            f"{fmt_num(m.get('aggregate_tps'))} | {fmt_num(m.get('per_stream_decode_tps_avg'))} | "
            f"{fmt_num(m.get('ttft_p50_s'))} | {fmt_num(m.get('ttft_p95_s'))} |"
        )
    lines.append("")
    return "\n".join(lines)


def prefix_cache_table(rows: dict[str, dict]) -> str:
    lines = [
        "### Prefix cache (latest per model)",
        "",
        "| Model | When | TTFT 1st (s) | TTFT 2nd (s) | Reduction % |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    items = []
    for model, sc in rows.items():
        row = sc.get("prefix-cache")
        if not row:
            continue
        m = row.get("metrics") or {}
        items.append((parse_ts(row.get("timestamp", "")), model, row, m))
    items.sort(key=lambda x: x[0], reverse=True)
    if not items:
        lines.append("| _no data_ | | | | |")
    for _, model, row, m in items:
        when = (row.get("timestamp") or "")[:19].replace("T", " ")
        lines.append(
            f"| `{model}` | {when} | {fmt_num(m.get('ttft_first_s'))} | "
            f"{fmt_num(m.get('ttft_second_s'))} | {fmt_num(m.get('ttft_reduction_pct'))} |"
        )
    lines.append("")
    return "\n".join(lines)


def mixed_table(rows: dict[str, dict]) -> str:
    lines = [
        "### Mixed brain + worker (latest)",
        "",
        "| Pair | When | Brain tok/s | Worker tok/s | Brain solo | Worker solo |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    items = []
    for model, sc in rows.items():
        row = sc.get("mixed")
        if not row:
            continue
        m = row.get("metrics") or {}
        items.append((parse_ts(row.get("timestamp", "")), model, row, m))
    items.sort(key=lambda x: x[0], reverse=True)
    if not items:
        lines.append("| _no data_ | | | | | |")
    for _, model, row, m in items:
        when = (row.get("timestamp") or "")[:19].replace("T", " ")
        lines.append(
            f"| `{model}` | {when} | {fmt_num(m.get('brain_decode_tps'))} | "
            f"{fmt_num(m.get('worker_decode_tps'))} | "
            f"{fmt_num(m.get('brain_solo_baseline_tps'))} | "
            f"{fmt_num(m.get('worker_solo_baseline_tps'))} |"
        )
    lines.append("")
    return "\n".join(lines)


def power_table(rows: dict[str, dict]) -> str:
    lines = [
        "### Power samples (latest per model)",
        "",
        "| Model | When | Context | Avg watts |",
        "| --- | --- | --- | ---: |",
    ]
    items = []
    for model, sc in rows.items():
        row = sc.get("power")
        if not row:
            continue
        m = row.get("metrics") or {}
        items.append((parse_ts(row.get("timestamp", "")), model, row, m))
    items.sort(key=lambda x: x[0], reverse=True)
    if not items:
        lines.append("| _no data_ | | | |")
    for _, model, row, m in items:
        when = (row.get("timestamp") or "")[:19].replace("T", " ")
        ctx = row.get("during") or "—"
        watts = m.get("avg_watts") if m.get("available") else m.get("error", "—")
        lines.append(f"| `{model}` | {when} | {ctx} | {fmt_num(watts)} |")
    lines.append("")
    return "\n".join(lines)


def host_summary(rows: list[dict]) -> str:
    if not rows:
        return ""
    latest = max(rows, key=lambda r: parse_ts(r.get("timestamp", "")))
    parts = []
    for k in ("chip", "ram_gb", "macos_version", "omlx_version"):
        if latest.get(k):
            parts.append(f"{k}={latest[k]}")
    omlx = latest.get("omlx") or {}
    if omlx.get("max_concurrent_requests") is not None:
        parts.append(f"max_concurrent_requests={omlx['max_concurrent_requests']}")
    if not parts:
        return ""
    return "**Latest host context:** " + ", ".join(parts) + "\n\n"


def build_readme(rows: list[dict], by_ms: dict[str, dict[str, dict]]) -> str:
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = [
        "# Benchmark results",
        "",
        f"_Generated by `make bench-report` on {generated}._",
        "",
        "Raw measurements live in [`results/`](results/) as JSONL (one file per bench run day/host).",
        "",
        host_summary(rows),
        single_stream_table(by_ms),
        concurrency_table(by_ms),
        prefix_cache_table(by_ms),
        mixed_table(by_ms),
        power_table(by_ms),
    ]
    return "\n".join(body)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Regenerate benchmarks/README.md from jsonl results.")
    p.add_argument(
        "--results-dir", default=RESULTS_DIR, help="directory containing *.jsonl (default benchmarks/results)",
    )
    p.add_argument(
        "--output", default=README_PATH, help="markdown output path (default benchmarks/README.md)",
    )
    return p.parse_args()


def main() -> None:
    global RESULTS_DIR, README_PATH
    args = parse_args()
    RESULTS_DIR = args.results_dir
    README_PATH = args.output

    rows = load_rows()
    by_ms = latest_by_model_scenario(rows)
    os.makedirs(os.path.dirname(README_PATH), exist_ok=True)
    content = build_readme(rows, by_ms)
    with open(README_PATH, "w", encoding="utf-8") as fh:
        fh.write(content)
    n_models = len(by_ms)
    print(f"wrote {README_PATH} ({len(rows)} row(s), {n_models} model(s))")


if __name__ == "__main__":
    main()
