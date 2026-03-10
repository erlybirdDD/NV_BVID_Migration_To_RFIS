#!/usr/bin/env python3
"""
Parse RFIS Shadow Read mismatch logs into agent-ready structured JSON.

Usage:
    python3 parse_rfis_logs.py <log_file> [<log_file2> ...]  # one JSON log entry per line
    cat *.log | python3 parse_rfis_logs.py -                 # stdin
    python3 parse_rfis_logs.py --pretty <log_file>           # pretty-print output
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional

DEFAULT_FOLDER = Path.home() / "Downloads" / "sangram_bvid_migration"


# ---------------------------------------------------------------------------
# Kotlin toString() parser
# ---------------------------------------------------------------------------

class KotlinToStringParser:
    """
    Parse Kotlin's default toString() format into Python dicts/lists.

    Handles:
      {key=value, key2={nested=obj}, key3=[list, items]}
    Values may be primitives (strings, ints, floats), null, true/false,
    nested objects, or nested lists.
    """

    def __init__(self, s: str):
        self.s = s
        self.pos = 0

    def parse(self) -> Any:
        self._skip_ws()
        return self._parse_value()

    def _skip_ws(self):
        while self.pos < len(self.s) and self.s[self.pos] in " \t\n\r":
            self.pos += 1

    def _parse_value(self) -> Any:
        self._skip_ws()
        if self.pos >= len(self.s):
            return None
        c = self.s[self.pos]
        if c == "{":
            return self._parse_object()
        if c == "[":
            return self._parse_list()
        return self._parse_primitive()

    def _parse_object(self) -> dict:
        self.pos += 1  # skip {
        result = {}
        while True:
            self._skip_ws()
            if self.pos >= len(self.s) or self.s[self.pos] == "}":
                self.pos += 1
                return result
            key = self._parse_key()
            self._skip_ws()
            if self.pos < len(self.s) and self.s[self.pos] == "=":
                self.pos += 1
            value = self._parse_value()
            result[key] = value
            self._skip_ws()
            if self.pos < len(self.s) and self.s[self.pos] == ",":
                self.pos += 1

    def _parse_key(self) -> str:
        start = self.pos
        while self.pos < len(self.s) and self.s[self.pos] not in "=,{}[] \t\n\r":
            self.pos += 1
        return self.s[start:self.pos]

    def _parse_list(self) -> list:
        self.pos += 1  # skip [
        result = []
        self._skip_ws()
        if self.pos < len(self.s) and self.s[self.pos] == "]":
            self.pos += 1
            return result
        while True:
            self._skip_ws()
            if self.pos >= len(self.s) or self.s[self.pos] == "]":
                if self.pos < len(self.s):
                    self.pos += 1
                return result
            value = self._parse_value()
            result.append(value)
            self._skip_ws()
            if self.pos < len(self.s) and self.s[self.pos] == ",":
                self.pos += 1

    def _parse_primitive(self) -> Any:
        start = self.pos
        depth = 0
        while self.pos < len(self.s):
            c = self.s[self.pos]
            if c in "{[":
                depth += 1
            elif c in "}]":
                if depth == 0:
                    break
                depth -= 1
            elif c == "," and depth == 0:
                break
            self.pos += 1
        raw = self.s[start:self.pos].strip()
        if raw == "null":
            return None
        if raw == "true":
            return True
        if raw == "false":
            return False
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            pass
        return raw


def parse_kotlin_list(s: str) -> list:
    try:
        return KotlinToStringParser(s).parse()
    except Exception as e:
        return [{"parse_error": str(e), "raw_preview": s[:300]}]


# ---------------------------------------------------------------------------
# Timestamp utilities
# ---------------------------------------------------------------------------

def _parse_iso(s: str) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def offset_minutes(a: Optional[str], b: Optional[str]) -> Optional[float]:
    """Return (a - b) in minutes, or None if either is unparseable."""
    dt_a = _parse_iso(a)
    dt_b = _parse_iso(b)
    if dt_a is None or dt_b is None:
        return None
    return (dt_a - dt_b).total_seconds() / 60


# ---------------------------------------------------------------------------
# Per-day summarisation
# ---------------------------------------------------------------------------

def summarise_day(day: dict) -> dict:
    ts = day.get("dayTimestamp", {})
    windows = day.get("timeWindows", [])
    first = windows[0] if windows else {}
    last = windows[-1] if windows else {}
    month = str(ts.get("month", "?")).zfill(2)
    day_n = str(ts.get("day", "?")).zfill(2)
    year = ts.get("year", "?")
    return {
        "date": f"{year}-{month}-{day_n}",
        "window_count": len(windows),
        "first_display": first.get("displayString"),
        "first_midpoint": first.get("midpointTimestamp"),
        "last_display": last.get("displayString"),
        "last_midpoint": last.get("midpointTimestamp"),
        "interval_minutes": first.get("intervalInMinutes"),
    }


# ---------------------------------------------------------------------------
# Comparison + pattern detection
# ---------------------------------------------------------------------------

def compare_days(shadow_days: list, original_days: list) -> dict:
    n = max(len(shadow_days), len(original_days))
    days = []
    for i in range(n):
        s = summarise_day(shadow_days[i]) if i < len(shadow_days) else None
        o = summarise_day(original_days[i]) if i < len(original_days) else None
        entry: dict = {"date": (s or o or {}).get("date")}
        if s:
            entry["shadow"] = {k: v for k, v in s.items() if k != "date"}
        if o:
            entry["original"] = {k: v for k, v in o.items() if k != "date"}
        if s and o:
            entry["count_diff"] = s["window_count"] - o["window_count"]
            entry["start_offset_minutes"] = offset_minutes(
                s["first_midpoint"], o["first_midpoint"]
            )
        days.append(entry)

    mismatched = [d for d in days if d.get("count_diff") not in (0, None)]
    same_count = [d for d in days if d.get("count_diff") == 0]
    offsets = [d["start_offset_minutes"] for d in days if d.get("start_offset_minutes") is not None]
    unique_offsets = list({round(x, 1) for x in offsets})

    # Pattern classification
    pattern, description = _classify_pattern(days, mismatched, unique_offsets)

    return {
        "days": days,
        "summary": {
            "total_days_compared": len(days),
            "days_with_count_mismatch": len(mismatched),
            "mismatched_dates": [d["date"] for d in mismatched],
            "days_with_same_count": len(same_count),
            "start_offset_minutes_observed": unique_offsets,
        },
        "pattern": pattern,
        "pattern_description": description,
    }


def _classify_pattern(days, mismatched, unique_offsets) -> tuple[str, str]:
    if not mismatched:
        return "no_count_mismatch", "Window counts match on all days; mismatch may be in display strings or timestamps only."

    all_off_by_one = all(abs(d.get("count_diff", 0)) == 1 for d in mismatched)
    consistent_offset = len(unique_offsets) == 1

    if all_off_by_one and consistent_offset and unique_offsets:
        offset = unique_offsets[0]
        direction = "earlier" if offset < 0 else "later"
        return (
            "consistent_time_offset_with_extra_window",
            f"Shadow windows start {abs(offset):.1f} min {direction} than original on all days. "
            f"This causes {len(mismatched)} day(s) to have 1 extra window "
            f"({[d['date'] for d in mismatched]}). "
            f"Likely a rounding/alignment difference in how the two systems anchor the first window."
        )

    if all_off_by_one:
        return (
            "off_by_one_window_inconsistent_offset",
            f"Window counts differ by 1 on {len(mismatched)} day(s) but start-time offsets are not consistent: {unique_offsets}."
        )

    return (
        "count_mismatch",
        f"Window counts differ by more than 1 on some days. Mismatched dates: {[d['date'] for d in mismatched]}."
    )


# ---------------------------------------------------------------------------
# nvDeliveryOptionResponse parsing
# ---------------------------------------------------------------------------

def summarise_nv_response(raw_str: str) -> Optional[dict]:
    """
    Parse the nvDeliveryOptionResponse JSON string and extract the fields
    most relevant for understanding the RFIS shadow data.
    """
    if not raw_str:
        return None
    try:
        data = json.loads(raw_str)
    except (json.JSONDecodeError, TypeError):
        return {"parse_error": "invalid JSON", "raw_preview": str(raw_str)[:200]}

    # nvDeliveryOptionResponse may be a list (one per store) or a single object
    if isinstance(data, list):
        data = data[0] if data else {}

    result: dict = {
        "response_identifier": data.get("deliveryOptionsResponseIdentifier"),
        "options": {},
    }

    for opt in data.get("newVerticalsDeliveryOptions", []):
        opt_type = opt.get("type", "UNKNOWN")
        display = opt.get("displayStrings", {})
        windows = opt.get("deliveryWindows", [])

        if "STANDARD" in opt_type:
            result["options"]["standard"] = {
                "eligibility": opt.get("eligibility"),
                "eta_subtitle": display.get("subtitleDisplayString"),
                "window_count": len(windows),
            }

        elif "SCHEDULED" in opt_type:
            # Group windows by calendar day
            by_day: dict = {}
            for w in windows:
                tw = w.get("timeWindow", {})
                day_ts = tw.get("dayTimestamp", {})
                day_key = "{year}-{month:02d}-{day:02d}".format(
                    year=day_ts.get("year", "?"),
                    month=int(day_ts.get("month", 0)),
                    day=int(day_ts.get("day", 0)),
                )
                by_day.setdefault(day_key, []).append(tw)

            first_w = windows[0].get("timeWindow", {}) if windows else {}
            last_w = windows[-1].get("timeWindow", {}) if windows else {}

            result["options"]["scheduled"] = {
                "eligibility": opt.get("eligibility"),
                "next_window_subtitle": display.get("subtitleDisplayString"),
                "total_window_count": len(windows),
                "first_window": {
                    "start": first_w.get("startTimestamp"),
                    "end": first_w.get("endTimestamp"),
                    "midpoint": first_w.get("midpointTimestamp"),
                    "display": first_w.get("displayStrings", {}).get("timeWindowCheckoutDisplayString"),
                    "day": first_w.get("dayTimestamp"),
                },
                "last_window": {
                    "start": last_w.get("startTimestamp"),
                    "end": last_w.get("endTimestamp"),
                    "midpoint": last_w.get("midpointTimestamp"),
                    "display": last_w.get("displayStrings", {}).get("timeWindowCheckoutDisplayString"),
                    "day": last_w.get("dayTimestamp"),
                },
                "windows_per_day": {day: len(wins) for day, wins in sorted(by_day.items())},
            }

    return result


# ---------------------------------------------------------------------------
# Log entry parsing
# ---------------------------------------------------------------------------

def parse_log_entry(raw: str) -> Optional[dict]:
    raw = raw.strip()
    if not raw:
        return None

    # Outer wrapper (Splunk/Datadog JSON export, or raw log line)
    try:
        outer = json.loads(raw)
    except json.JSONDecodeError:
        return None

    # Inner message may be a JSON string embedded in the 'message' field
    msg_str = outer.get("message", "")
    if isinstance(msg_str, str) and msg_str.lstrip().startswith("{"):
        try:
            inner = json.loads(msg_str)
        except json.JSONDecodeError:
            inner = outer
    else:
        inner = outer

    if "RFIS Shadow Read" not in str(inner.get("message", "")):
        return None

    shadow_str = inner.get("availableDays_shadow", "")
    original_str = inner.get("availableDays_original", "")
    shadow_days = parse_kotlin_list(shadow_str) if shadow_str else []
    original_days = parse_kotlin_list(original_str) if original_str else []

    comparison = compare_days(shadow_days, original_days)
    nv_response = summarise_nv_response(inner.get("nvDeliveryOptionResponse"))

    return {
        "trace_id": inner.get("trace_id"),
        "timestamp": outer.get("@timestamp") or outer.get("time") or outer.get("timestamp"),
        "metadata": {
            "store_id": inner.get("storeId"),
            "business_vertical_id": inner.get("businessVerticalId"),
            "submarket_id": inner.get("submarketId"),
            "consumer_id": inner.get("consumerId"),
            "endpoint": inner.get("ep"),
            "logger": inner.get("logger_name"),
        },
        "rfis_response": nv_response,
        "comparison": comparison,
    }


# ---------------------------------------------------------------------------
# File / folder ingestion
# ---------------------------------------------------------------------------

def collect_paths(inputs: list[str], recursive: bool) -> list[Path]:
    """
    Expand a list of file/directory path strings into a sorted list of
    individual .json file Paths.  Directories are walked; files are used as-is.
    """
    paths: list[Path] = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            glob = p.rglob("*.json") if recursive else p.glob("*.json")
            paths.extend(sorted(glob))
        else:
            paths.append(p)
    return paths


def process_file(path: Path, results: list) -> None:
    """
    Parse a single file.  Tries two strategies in order:
      1. The file is one JSON object (pretty-printed or minified).
      2. The file is NDJSON — one JSON object per line.
    """
    text = path.read_text(encoding="utf-8", errors="replace")

    # Strategy 1: whole file is a single JSON object
    try:
        obj = json.loads(text)
        entry = parse_log_entry(json.dumps(obj))  # normalise back to a string
        if entry:
            entry.setdefault("source_file", str(path))
            results.append(entry)
        return
    except json.JSONDecodeError:
        pass

    # Strategy 2: NDJSON (one JSON object per line)
    for line in text.splitlines():
        entry = parse_log_entry(line)
        if entry:
            entry.setdefault("source_file", str(path))
            results.append(entry)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Normalize RFIS Shadow Read mismatch logs to structured JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  # default folder (~/{DEFAULT_FOLDER.relative_to(Path.home())})
  python3 parse_rfis_logs.py

  # specific folder (recursive)
  python3 parse_rfis_logs.py --folder ~/Downloads/sangram_bvid_migration

  # only a subfolder
  python3 parse_rfis_logs.py --folder ~/Downloads/sangram_bvid_migration/by_issue_type/01_large_diff_original_bigger

  # explicit files
  python3 parse_rfis_logs.py log_0001.json log_0002.json

  # stdin (NDJSON stream)
  cat *.json | python3 parse_rfis_logs.py -
""",
    )
    ap.add_argument(
        "files",
        nargs="*",
        help="Log files or directories (default: %(default)s)",
    )
    ap.add_argument(
        "--folder", "-d",
        default=None,
        help=f"Directory to read .json files from (default: {DEFAULT_FOLDER})",
    )
    ap.add_argument(
        "--recurse", "-r",
        action="store_true",
        help="Recurse into subdirectories when scanning a folder (default: top-level only)",
    )
    ap.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = ap.parse_args()

    results: list = []
    recursive = args.recurse

    if args.files == ["-"]:
        # Explicit stdin mode: NDJSON stream, write to stdout only
        for line in sys.stdin:
            entry = parse_log_entry(line)
            if entry:
                results.append(entry)
        print(json.dumps(results, indent=2 if args.pretty else None, default=str))
    else:
        paths = []
        if args.files:
            paths = collect_paths(args.files, recursive)
        else:
            folder = Path(args.folder) if args.folder else DEFAULT_FOLDER
            paths = collect_paths([str(folder)], recursive)

        skipped = 0
        for path in paths:
            file_results: list = []
            process_file(path, file_results)
            if file_results:
                out_path = path.parent / f"{path.stem}_cleaned.json"
                out_path.write_text(
                    json.dumps(
                        file_results[0] if len(file_results) == 1 else file_results,
                        indent=2,
                        default=str,
                    ),
                    encoding="utf-8",
                )
                print(f"  wrote {out_path}", file=sys.stderr)
            else:
                skipped += 1
            results.extend(file_results)

        print(
            f"\nDone: {len(results)} entries parsed from {len(paths) - skipped} files "
            f"({skipped} skipped — no RFIS mismatch found).",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
