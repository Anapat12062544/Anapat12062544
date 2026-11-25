#!/usr/bin/env python3
"""Build activity-2026.svg from GitHub weekday contributions.

Reads GH_STATS_TOKEN, queries contributionsCollection.contributionCalendar for
Anapat12062544 from 2026-01-01 through today (capped at 2026-12-31), drops
Saturday and Sunday, and writes a dark line chart at the repo root.

Pass --placeholder to write a synthetic sample chart without calling GitHub.
That chart is not real contribution data.
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from xml.sax.saxutils import escape

LOGIN = "Anapat12062544"
API_URL = "https://api.github.com/graphql"
YEAR_START = date(2026, 1, 1)
YEAR_END = date(2026, 12, 31)
TITLE = "Anapat Hengko's Contributions 2026 (Mon–Fri)"
PLACEHOLDER_SUBTITLE = "waiting for first run"
MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(REPO_ROOT, "activity-2026.svg")

QUERY = """
query ($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        weeks {
          contributionDays {
            date
            contributionCount
          }
        }
      }
    }
  }
}
""".strip()


class ActivityGraphError(Exception):
    """The chart could not be built. The message is safe to show as-is."""


def query_window(now: datetime) -> tuple[date, date, str, str]:
    """Return the inclusive chart dates plus GraphQL from/to timestamps.

    `to` is never in the future. While the window is still inside 2026, it is
    the current UTC time (pulled back a few seconds so clock skew cannot make
    GitHub reject it). After 2026 it is capped at 2026-12-31.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    start = YEAR_START
    end = min(now.date(), YEAR_END)
    if end < start:
        raise ActivityGraphError(
            "the contribution window starts on 2026-01-01, "
            f"but today is {now.date().isoformat()}"
        )

    from_iso = f"{start.isoformat()}T00:00:00Z"
    if end < now.date():
        to_iso = f"{end.isoformat()}T23:59:59Z"
    else:
        stamp = now - timedelta(seconds=30)
        if stamp.date() < start:
            raise ActivityGraphError(
                "the contribution window is empty because the current UTC time "
                "is not after 2026-01-01T00:00:00Z"
            )
        if stamp.date() < end:
            stamp = datetime(end.year, end.month, end.day, 0, 0, 1, tzinfo=timezone.utc)
        to_iso = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    if to_iso <= from_iso:
        raise ActivityGraphError(
            "the contribution window is empty because the current UTC time "
            "is not after 2026-01-01T00:00:00Z"
        )
    return start, end, from_iso, to_iso


def weekday_series(
    counts: dict[date, int], start: date, end: date
) -> list[tuple[date, int]]:
    series: list[tuple[date, int]] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            series.append((current, counts.get(current, 0)))
        current += timedelta(days=1)
    if not series:
        raise ActivityGraphError(
            f"no Monday–Friday days between {start.isoformat()} and {end.isoformat()}"
        )
    return series


def parse_contribution_days(
    payload: dict, start: date, end: date
) -> list[tuple[date, int]]:
    if not isinstance(payload, dict):
        raise ActivityGraphError(
            "GitHub GraphQL API returned a response that is not a JSON object"
        )

    errors = payload.get("errors")
    if errors:
        messages: list[str] = []
        for err in errors:
            if isinstance(err, dict) and err.get("message"):
                messages.append(str(err["message"]))
            else:
                messages.append(str(err))
        detail = "; ".join(messages) if messages else "unknown error"
        raise ActivityGraphError(f"GitHub GraphQL API returned errors: {detail}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ActivityGraphError("GitHub GraphQL API returned no data")

    user = data.get("user")
    if not isinstance(user, dict):
        raise ActivityGraphError(
            f"GitHub GraphQL API returned no user named {LOGIN}"
        )

    collection = user.get("contributionsCollection")
    if not isinstance(collection, dict):
        raise ActivityGraphError(
            "GitHub GraphQL API response did not include contributionsCollection"
        )

    calendar = collection.get("contributionCalendar")
    if not isinstance(calendar, dict) or "weeks" not in calendar:
        raise ActivityGraphError(
            "GitHub GraphQL API response did not include "
            "contributionsCollection.contributionCalendar"
        )

    weeks = calendar.get("weeks")
    if not isinstance(weeks, list):
        raise ActivityGraphError("GitHub contributionCalendar.weeks was not a list")

    counts: dict[date, int] = {}
    saw_day = False
    for week in weeks:
        if not isinstance(week, dict):
            continue
        days = week.get("contributionDays") or []
        if not isinstance(days, list):
            raise ActivityGraphError(
                "GitHub contributionCalendar returned a week without a day list"
            )
        for day in days:
            if not isinstance(day, dict) or "date" not in day:
                raise ActivityGraphError(
                    "GitHub contributionCalendar returned a day without a date"
                )
            saw_day = True
            raw_date = str(day["date"])
            try:
                current = date.fromisoformat(raw_date)
            except ValueError as exc:
                raise ActivityGraphError(
                    f"GitHub returned an invalid contribution date: {raw_date}"
                ) from exc
            if current < start or current > end or current.weekday() >= 5:
                continue
            try:
                count = int(day.get("contributionCount", 0))
            except (TypeError, ValueError) as exc:
                raise ActivityGraphError(
                    f"GitHub returned an invalid contributionCount for {current.isoformat()}"
                ) from exc
            if count < 0:
                raise ActivityGraphError(
                    f"GitHub returned a negative contributionCount for {current.isoformat()}"
                )
            counts[current] = count

    if not saw_day:
        raise ActivityGraphError(
            "GitHub contribution calendar returned no days for "
            f"{start.isoformat()} through {end.isoformat()}"
        )
    return weekday_series(counts, start, end)


def github_graphql(token: str, variables: dict[str, str]) -> dict:
    body = json.dumps({"query": QUERY, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "anapat-activity-graph",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        snippet = " ".join(detail.split())[:500] if detail else str(exc.reason)
        raise ActivityGraphError(
            f"GitHub GraphQL API returned HTTP {exc.code}: {snippet}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ActivityGraphError(
            f"GitHub GraphQL API request failed: {exc.reason}"
        ) from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ActivityGraphError("GitHub GraphQL API returned non-JSON") from exc
    if not isinstance(payload, dict):
        raise ActivityGraphError(
            "GitHub GraphQL API returned a response that is not a JSON object"
        )
    return payload


def fetch_series(token: str, now: datetime) -> list[tuple[date, int]]:
    start, end, from_iso, to_iso = query_window(now)
    payload = github_graphql(
        token,
        {"login": LOGIN, "from": from_iso, "to": to_iso},
    )
    return parse_contribution_days(payload, start, end)


def synthetic_placeholder_series(now: datetime) -> list[tuple[date, int]]:
    """A regular triangle wave. These values are not contribution counts."""
    start, end, _from_iso, _to_iso = query_window(now)
    series: list[tuple[date, int]] = []
    index = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            cycle = index % 16
            value = cycle if cycle <= 8 else 16 - cycle
            series.append((current, value))
            index += 1
        current += timedelta(days=1)
    if not series:
        raise ActivityGraphError(
            f"no Monday–Friday days between {start.isoformat()} and {end.isoformat()}"
        )
    return series


def axis_ticks(max_value: int) -> list[int]:
    if max_value < 0:
        raise ActivityGraphError("contribution counts cannot be negative")
    if max_value == 0:
        return [0, 1, 2, 3, 4]

    intervals = 4
    raw_step = max_value / intervals
    magnitude = 10 ** math.floor(math.log10(raw_step))
    for factor in (1, 2, 2.5, 5, 10):
        step = factor * magnitude
        if step * intervals + 1e-9 < max_value:
            continue
        ticks = [step * i for i in range(intervals + 1)]
        if all(abs(tick - round(tick)) < 1e-9 for tick in ticks):
            return [int(round(tick)) for tick in ticks]

    step_i = math.ceil(max_value / intervals)
    return [step_i * i for i in range(intervals + 1)]


def contribution_subtitle(series: list[tuple[date, int]]) -> str:
    total = sum(count for _day, count in series)
    return f"{total:,} contributions · {len(series):,} workdays"


def render_svg(
    series: list[tuple[date, int]],
    *,
    subtitle: str,
    placeholder: bool = False,
) -> str:
    if not series:
        raise ActivityGraphError("cannot render an empty contribution series")

    width = 1200
    height = 420
    left = 78
    right = 32
    top = 92
    bottom = 48
    plot_w = width - left - right
    plot_h = height - top - bottom
    baseline = top + plot_h

    ticks = axis_ticks(max(count for _day, count in series))
    y_max = ticks[-1]
    count = len(series)

    def x_at(index: int) -> float:
        if count == 1:
            return left + plot_w / 2
        return left + (index / (count - 1)) * plot_w

    def y_at(value: int) -> float:
        return baseline - (value / y_max) * plot_h

    line_points = " ".join(
        f"{x_at(i):.2f},{y_at(value):.2f}" for i, (_day, value) in enumerate(series)
    )
    area_points = (
        f"{x_at(0):.2f},{baseline:.2f} "
        + line_points
        + f" {x_at(count - 1):.2f},{baseline:.2f}"
    )

    month_labels: list[tuple[float, str]] = []
    month_lines: list[float] = []
    seen_months: list[tuple[int, int]] = []
    indexes_by_month: dict[tuple[int, int], list[int]] = {}
    for index, (day, _value) in enumerate(series):
        key = (day.year, day.month)
        indexes_by_month.setdefault(key, []).append(index)
        if key not in seen_months:
            seen_months.append(key)
    for position, key in enumerate(seen_months):
        indexes = indexes_by_month[key]
        mid = x_at(indexes[len(indexes) // 2])
        month_labels.append((mid, MONTHS[key[1] - 1]))
        if position > 0:
            month_lines.append(x_at(indexes[0]))

    def text(
        x: float,
        y: float,
        content: str,
        size: int,
        fill: str,
        anchor: str = "start",
        weight: str = "400",
    ) -> str:
        return (
            f'<text x="{x:.2f}" y="{y:.2f}" fill="{fill}" font-size="{size}" '
            f'font-family="\'Segoe UI\', Ubuntu, Sans-Serif" font-weight="{weight}" '
            f'text-anchor="{anchor}" dominant-baseline="middle">{escape(content)}</text>'
        )

    grid: list[str] = []
    labels: list[str] = []
    for tick in ticks:
        y = y_at(tick)
        grid.append(
            f'<line x1="{left:.2f}" y1="{y:.2f}" x2="{left + plot_w:.2f}" y2="{y:.2f}" '
            'stroke="#8b949e" stroke-opacity="0.3" stroke-width="1" '
            'stroke-dasharray="2 4"/>'
        )
        labels.append(text(left - 12, y, f"{tick:,}", 12, "#8b949e", "end"))

    for x in month_lines:
        grid.append(
            f'<line x1="{x:.2f}" y1="{top:.2f}" x2="{x:.2f}" y2="{baseline:.2f}" '
            'stroke="#8b949e" stroke-opacity="0.2" stroke-width="1" '
            'stroke-dasharray="2 4"/>'
        )

    month_svg = "\n    ".join(
        text(min(max(x, left + 18), left + plot_w - 18), baseline + 24, label, 13, "#8b949e", "middle")
        for x, label in month_labels
    )
    label_svg = "\n    ".join(labels)
    grid_svg = "\n    ".join(grid)

    description = (
        "Placeholder chart using a synthetic triangle wave. "
        "These values are not GitHub contribution counts. waiting for first run"
        if placeholder
        else f"Monday–Friday GitHub contributions in 2026 for {LOGIN}. {subtitle}"
    )
    comment = (
        "<!-- placeholder: synthetic sample data, not real GitHub contributions -->\n"
        if placeholder
        else ""
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
{comment}<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="420" viewBox="0 0 1200 420" role="img" aria-labelledby="chart-title chart-desc">
  <title id="chart-title">{escape(TITLE)}</title>
  <desc id="chart-desc">{escape(description)}</desc>
  <rect width="1200" height="420" rx="8" fill="#0d1117" stroke="#30363d"/>
  <defs>
    <linearGradient id="areaFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#26a641" stop-opacity="0.45"/>
      <stop offset="1" stop-color="#26a641" stop-opacity="0.02"/>
    </linearGradient>
  </defs>
  {text(width / 2, 36, TITLE, 20, "#c9d1d9", "middle", "600")}
  {text(width / 2, 64, subtitle, 14, "#8b949e", "middle")}
  <g font-family="'Segoe UI', Ubuntu, Sans-Serif">
    {grid_svg}
    <polygon points="{area_points}" fill="url(#areaFill)"/>
    <polyline points="{line_points}" fill="none" stroke="#26a641" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>
    {label_svg}
    {month_svg}
  </g>
</svg>
"""


def write_svg(svg: str, path: str = OUTPUT_PATH) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(svg)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    placeholder = False
    if "--placeholder" in args:
        placeholder = True
        args = [arg for arg in args if arg != "--placeholder"]
    if args:
        print(
            "error: unknown arguments: "
            + " ".join(args)
            + ". Usage: activity_graph.py [--placeholder]",
            file=sys.stderr,
        )
        return 1

    try:
        now = datetime.now(timezone.utc)
        if placeholder:
            series = synthetic_placeholder_series(now)
            subtitle = PLACEHOLDER_SUBTITLE
        else:
            token = os.environ.get("GH_STATS_TOKEN", "").strip()
            if not token:
                raise ActivityGraphError(
                    "GH_STATS_TOKEN is not set. Add a classic personal access token "
                    "with the read:user and repo scopes as the GH_STATS_TOKEN secret, "
                    "then run this script again."
                )
            series = fetch_series(token, now)
            subtitle = contribution_subtitle(series)
        svg = render_svg(series, subtitle=subtitle, placeholder=placeholder)
        write_svg(svg)
    except ActivityGraphError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    kind = "placeholder" if placeholder else "contributions"
    print(f"wrote {OUTPUT_PATH} ({kind}, {len(series)} workdays)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
