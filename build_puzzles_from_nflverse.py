#!/usr/bin/env python3
"""Build puzzle and player profile data from nflverse player stats.

Default behavior:
- Pulls seasonal nflverse stats_player_week CSV files from GitHub releases
- Filters to REG season, WR/RB/TE, and PPR >= 35
- Writes:
  - data/puzzles.json
  - data/player_profiles.json
  - data/qa_report.json
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import ssl
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SEASON_URL_TEMPLATE = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.{ext}"

NAME_KEYS = ["player_display_name", "player_name", "name", "player"]
DATE_KEYS = ["game_date", "gameday", "date"]
SEASON_KEYS = ["season", "game_season"]
WEEK_KEYS = ["week", "game_week"]
PPR_KEYS = ["fantasy_points_ppr", "fantasy_points_ppr_player", "fantasy_ppr", "ppr"]
POS_KEYS = ["position_group", "position"]
SEASON_TYPE_KEYS = ["season_type", "game_type", "type"]
TEAM_KEYS = ["recent_team", "team", "posteam"]
OPP_KEYS = ["opponent_team", "opponent", "defteam"]


def first_non_empty(row: dict[str, str], keys: list[str], default: str = "") -> str:
    for key in keys:
        val = row.get(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return default


def to_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: str, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def normalize_date(value: str) -> str:
    if not value:
        return ""
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def derive_display_date(row: dict[str, str]) -> str:
    season = first_non_empty(row, SEASON_KEYS)
    week = first_non_empty(row, WEEK_KEYS)
    if season and week:
        return f"Season {season} Week {week}"
    if season:
        return f"Season {season}"
    return "Unknown Date"


def open_csv_reader(url: str, insecure: bool = False) -> csv.DictReader:
    req = Request(url, headers={"User-Agent": "FantasyStatLineDaily/1.0"})
    ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    resp = urlopen(req, timeout=60, context=ctx)
    content_type = (resp.headers.get("Content-Type") or "").lower()
    path_lower = url.lower()
    raw = resp.read()

    if path_lower.endswith(".gz") or "gzip" in content_type:
        text = gzip.decompress(raw).decode("utf-8", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")

    lines = text.splitlines()
    return csv.DictReader(lines)


def load_rows(season_start: int, season_end: int, insecure: bool = False) -> list[dict[str, str]]:
    all_rows: list[dict[str, str]] = []
    failed_seasons: list[tuple[int, str]] = []

    for season in range(season_start, season_end + 1):
        loaded_this_season = False
        last_error: Exception | None = None
        for ext in ("csv", "csv.gz"):
            url = SEASON_URL_TEMPLATE.format(season=season, ext=ext)
            try:
                print(f"Trying {url} ...", file=sys.stderr)
                reader = open_csv_reader(url, insecure=insecure)
                rows = list(reader)
                if rows:
                    all_rows.extend(rows)
                    loaded_this_season = True
                    print(f"Loaded {len(rows)} rows from season {season}", file=sys.stderr)
                    break
            except (HTTPError, URLError, TimeoutError, OSError, gzip.BadGzipFile) as err:
                last_error = err
                continue
        if not loaded_this_season:
            failed_seasons.append((season, str(last_error or "unknown error")))

    if not all_rows:
        raise RuntimeError(
            "Could not load any seasonal nflverse stats files. "
            "Verify access to https://github.com/nflverse/nflverse-data/releases"
        )

    if failed_seasons:
        print(f"Warning: skipped {len(failed_seasons)} seasons due to download errors.", file=sys.stderr)

    return all_rows


def build_puzzles(rows: list[dict[str, str]], min_ppr: float, positions: set[str]) -> list[dict[str, object]]:
    puzzles: list[dict[str, object]] = []
    seen: set[tuple[str, str, float]] = set()

    for row in rows:
        season_type = first_non_empty(row, SEASON_TYPE_KEYS, "REG").upper()
        if season_type and season_type not in {"REG", "REGULAR"}:
            continue

        pos = first_non_empty(row, POS_KEYS, "").upper()
        if positions and pos not in positions:
            continue

        ppr = to_float(first_non_empty(row, PPR_KEYS, "0"), 0.0)
        if ppr < min_ppr:
            continue

        player = first_non_empty(row, NAME_KEYS)
        if not player:
            continue

        date = derive_display_date(row)
        season = first_non_empty(row, SEASON_KEYS, "")
        week = first_non_empty(row, WEEK_KEYS, "")

        team = first_non_empty(row, TEAM_KEYS, "UNK").upper()
        opp = first_non_empty(row, OPP_KEYS, "UNK").upper()
        matchup = f"{team} vs {opp}"

        receptions = to_int(row.get("receptions", "0"))
        rec_yds = to_int(row.get("receiving_yards", "0"))
        rec_td = to_int(row.get("receiving_tds", "0"))
        rush_yds = to_int(row.get("rushing_yards", "0"))
        rush_td = to_int(row.get("rushing_tds", "0"))

        stats = [
            {"label": "Receptions", "value": receptions},
            {"label": "Rec Yards", "value": rec_yds},
            {"label": "Rec TD", "value": rec_td},
            {"label": "Rush Yards", "value": rush_yds},
            {"label": "Rush TD", "value": rush_td},
        ]

        key = (player.lower(), date, round(ppr, 2))
        if key in seen:
            continue
        seen.add(key)

        puzzles.append(
            {
                "player": player,
                "date": date,
                "matchup": matchup,
                "team": team,
                "opponent": opp,
                "position": pos or "UNK",
                "season": int(season) if season.isdigit() else season,
                "week": int(week) if week.isdigit() else week,
                "ppr": round(ppr, 2),
                "stats": stats,
            }
        )

    puzzles.sort(key=lambda x: (x["date"], float(x["ppr"])), reverse=True)
    return puzzles


def normalize_name(value: str) -> str:
    return " ".join(value.lower().split())


SKILL_PROFILE_POSITIONS = {"WR", "RB", "TE"}


def pick_preferred_position(pos_counts: Counter[str]) -> str:
    if not pos_counts:
        return "UNK"
    skill_counts = {p: c for p, c in pos_counts.items() if p in SKILL_PROFILE_POSITIONS}
    source = skill_counts if skill_counts else dict(pos_counts)
    return sorted(source.items(), key=lambda item: (-item[1], item[0]))[0][0]


def build_player_profiles(rows: list[dict[str, str]]) -> dict[str, object]:
    aggregate: dict[str, dict[str, object]] = {}

    for row in rows:
        season_type = first_non_empty(row, SEASON_TYPE_KEYS, "REG").upper()
        if season_type and season_type not in {"REG", "REGULAR"}:
            continue

        player = first_non_empty(row, NAME_KEYS)
        if not player:
            continue

        pos = first_non_empty(row, POS_KEYS, "").upper()

        team = first_non_empty(row, TEAM_KEYS, "").upper()
        season = first_non_empty(row, SEASON_KEYS, "")
        season_key = season if season.isdigit() else ""
        norm = normalize_name(player)

        if norm not in aggregate:
            aggregate[norm] = {
                "player": player,
                "team_counts": Counter(),
                "pos_counts": Counter(),
                "pos_team_counts": defaultdict(Counter),
                "season_team_counts": defaultdict(Counter),
                "season_pos_counts": defaultdict(Counter),
                "season_pos_team_counts": defaultdict(lambda: defaultdict(Counter)),
            }

        entry = aggregate[norm]
        if team:
            entry["team_counts"][team] += 1
            if season_key:
                entry["season_team_counts"][season_key][team] += 1
        if pos:
            entry["pos_counts"][pos] += 1
            if team:
                entry["pos_team_counts"][pos][team] += 1
            if season_key:
                entry["season_pos_counts"][season_key][pos] += 1
                if team:
                    entry["season_pos_team_counts"][season_key][pos][team] += 1

    profiles: dict[str, object] = {}
    for norm, entry in aggregate.items():
        default_pos = pick_preferred_position(entry["pos_counts"])
        if default_pos in entry["pos_team_counts"] and entry["pos_team_counts"][default_pos]:
            default_team = entry["pos_team_counts"][default_pos].most_common(1)[0][0]
        else:
            default_team = entry["team_counts"].most_common(1)[0][0] if entry["team_counts"] else "UNK"

        by_season: dict[str, dict[str, str]] = {}
        seasons = sorted(
            set(entry["season_team_counts"].keys()) | set(entry["season_pos_counts"].keys()),
            key=lambda s: int(s),
        )
        for season in seasons:
            season_pos_counts = entry["season_pos_counts"][season]
            team = (
                entry["season_team_counts"][season].most_common(1)[0][0]
                if entry["season_team_counts"][season]
                else default_team
            )
            pos = pick_preferred_position(season_pos_counts) if season_pos_counts else default_pos
            if (
                season in entry["season_pos_team_counts"]
                and pos in entry["season_pos_team_counts"][season]
                and entry["season_pos_team_counts"][season][pos]
            ):
                team = entry["season_pos_team_counts"][season][pos].most_common(1)[0][0]
            by_season[season] = {"team": team, "position": pos}

        profiles[norm] = {
            "player": entry["player"],
            "default": {"team": default_team, "position": default_pos},
            "by_season": by_season,
        }

    return profiles


def build_search_players(rows: list[dict[str, str]], positions: set[str]) -> list[str]:
    names: set[str] = set()
    for row in rows:
        season_type = first_non_empty(row, SEASON_TYPE_KEYS, "REG").upper()
        if season_type and season_type not in {"REG", "REGULAR"}:
            continue

        pos = first_non_empty(row, POS_KEYS, "").upper()
        if positions and pos not in positions:
            continue

        player = first_non_empty(row, NAME_KEYS)
        if not player:
            continue
        names.add(player)

    return sorted(names)


def evaluate_quality(puzzles: list[dict[str, object]], profiles: dict[str, object]) -> dict[str, object]:
    required_stats = {"Receptions", "Rec Yards", "Rec TD", "Rush Yards", "Rush TD"}
    duplicate_keys: set[tuple[object, ...]] = set()
    seen_keys: set[tuple[object, ...]] = set()
    missing_team = 0
    missing_position = 0
    missing_stats = 0
    missing_season = 0
    by_season: Counter[str] = Counter()

    for puzzle in puzzles:
        team = str(puzzle.get("team", "")).upper()
        pos = str(puzzle.get("position", "")).upper()
        season = puzzle.get("season")
        season_key = str(season) if season not in (None, "", "UNK") else "UNKNOWN"
        by_season[season_key] += 1

        if team in {"", "UNK", "—"}:
            missing_team += 1
        if pos in {"", "UNK", "—"}:
            missing_position += 1
        if season_key == "UNKNOWN":
            missing_season += 1

        labels = {str(s.get("label", "")) for s in puzzle.get("stats", []) if isinstance(s, dict)}
        if not required_stats.issubset(labels):
            missing_stats += 1

        dedupe_key = (
            str(puzzle.get("player", "")).lower(),
            puzzle.get("season"),
            puzzle.get("week"),
            str(puzzle.get("team", "")),
            str(puzzle.get("opponent", "")),
            puzzle.get("ppr"),
        )
        if dedupe_key in seen_keys:
            duplicate_keys.add(dedupe_key)
        seen_keys.add(dedupe_key)

    profiles_without_default = 0
    profiles_without_seasons = 0
    for profile in profiles.values():
        default = profile.get("default", {}) if isinstance(profile, dict) else {}
        team = str(default.get("team", "")).upper()
        pos = str(default.get("position", "")).upper()
        by_season_obj = profile.get("by_season", {}) if isinstance(profile, dict) else {}
        if team in {"", "UNK", "—"} or pos in {"", "UNK", "—"}:
            profiles_without_default += 1
        if not isinstance(by_season_obj, dict) or not by_season_obj:
            profiles_without_seasons += 1

    issues = {
        "missing_team": missing_team,
        "missing_position": missing_position,
        "missing_required_stats": missing_stats,
        "missing_season": missing_season,
        "duplicate_puzzle_keys": len(duplicate_keys),
        "profiles_without_default_team_or_position": profiles_without_default,
        "profiles_without_season_history": profiles_without_seasons,
    }

    critical_issue_keys = [
        "missing_required_stats",
        "duplicate_puzzle_keys",
        "missing_season",
    ]
    strict_fail = any(issues[key] > 0 for key in critical_issue_keys)

    return {
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "summary": {
            "puzzle_count": len(puzzles),
            "profile_count": len(profiles),
            "season_count": len(by_season),
        },
        "issues": issues,
        "strict_criteria": critical_issue_keys,
        "season_distribution": dict(sorted(by_season.items(), key=lambda kv: kv[0])),
        "strict_fail": strict_fail,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate fantasy daily puzzles from nflverse stats")
    parser.add_argument("--min-ppr", type=float, default=35.0, help="Minimum PPR points (default: 35)")
    parser.add_argument(
        "--positions",
        default="WR,RB,TE",
        help="Comma-separated positions to include (default: WR,RB,TE)",
    )
    parser.add_argument(
        "--out",
        default="data/puzzles.json",
        help="Output JSON path (default: data/puzzles.json)",
    )
    parser.add_argument(
        "--profiles-out",
        default="data/player_profiles.json",
        help="Output JSON path for player profile mappings (default: data/player_profiles.json)",
    )
    parser.add_argument(
        "--search-out",
        default="data/player_search.json",
        help="Output JSON path for skill-player search names (default: data/player_search.json)",
    )
    parser.add_argument(
        "--qa-report-out",
        default="data/qa_report.json",
        help="Output JSON path for quality report (default: data/qa_report.json)",
    )
    parser.add_argument(
        "--season-start",
        type=int,
        default=2010,
        help="First season to include (default: 2010)",
    )
    parser.add_argument(
        "--season-end",
        type=int,
        default=datetime.now().year - 1,
        help="Last season to include (default: previous year)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL certificate verification for data download (use only if needed).",
    )
    parser.add_argument(
        "--strict-qa",
        action="store_true",
        help="Exit non-zero if QA report finds any issue counts above zero.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    positions = {p.strip().upper() for p in args.positions.split(",") if p.strip()}
    if args.season_end < args.season_start:
        print("--season-end must be >= --season-start", file=sys.stderr)
        return 1

    rows = load_rows(args.season_start, args.season_end, insecure=args.insecure)
    puzzles = build_puzzles(rows, min_ppr=args.min_ppr, positions=positions)

    if not puzzles:
        print("No puzzles generated. Try lowering --min-ppr or expanding --positions.", file=sys.stderr)
        return 1

    out_path = args.out
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(puzzles, f, indent=2)

    profiles = build_player_profiles(rows)
    profiles_out_path = args.profiles_out
    os.makedirs(os.path.dirname(profiles_out_path) or ".", exist_ok=True)
    with open(profiles_out_path, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2)

    search_players = build_search_players(rows, positions={"WR", "RB", "TE"})
    search_out_path = args.search_out
    os.makedirs(os.path.dirname(search_out_path) or ".", exist_ok=True)
    with open(search_out_path, "w", encoding="utf-8") as f:
        json.dump(search_players, f, indent=2)

    qa_report = evaluate_quality(puzzles, profiles)
    qa_out_path = args.qa_report_out
    os.makedirs(os.path.dirname(qa_out_path) or ".", exist_ok=True)
    with open(qa_out_path, "w", encoding="utf-8") as f:
        json.dump(qa_report, f, indent=2)

    print(f"Wrote {len(puzzles)} puzzles to {out_path}")
    print(f"Wrote {len(profiles)} player profiles to {profiles_out_path}")
    print(f"Wrote {len(search_players)} search players to {search_out_path}")
    print(f"Wrote QA report to {qa_out_path}")
    if args.strict_qa and qa_report.get("strict_fail", False):
        print("Strict QA failed. See qa_report issues.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
