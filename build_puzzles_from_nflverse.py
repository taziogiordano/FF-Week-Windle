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

SEASON_URL_TEMPLATES = [
    "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.{ext}",
    "https://github.com/nflverse/nflverse-data/releases/download/player_stats/player_stats_{season}.{ext}",
    "https://nflreadr.nflverse.com/data/stats_player_week_{season}.{ext}",
    "https://nflreadr.nflverse.com/data/player_stats/player_stats_{season}.{ext}",
    "https://nflverse-data.s3.amazonaws.com/stats_player_week_{season}.{ext}",
    "https://nflverse-data.s3.amazonaws.com/player_stats_{season}.{ext}",
]
PLAYER_META_URLS = [
    "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv",
    "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv.gz",
    "https://nflreadr.nflverse.com/data/players.csv",
    "https://nflreadr.nflverse.com/data/players.csv.gz",
    "https://nflverse-data.s3.amazonaws.com/players.csv",
    "https://nflverse-data.s3.amazonaws.com/players.csv.gz",
]

NAME_KEYS = ["player_display_name", "player_name", "name", "player"]
PLAYER_ID_KEYS = ["player_id", "gsis_id", "nfl_id", "pfr_id", "espn_id"]
DATE_KEYS = ["game_date", "gameday", "date"]
SEASON_KEYS = ["season", "game_season"]
WEEK_KEYS = ["week", "game_week"]
PPR_KEYS = ["fantasy_points_ppr", "fantasy_points_ppr_player", "fantasy_ppr", "ppr"]
POS_KEYS = ["position_group", "position"]
SEASON_TYPE_KEYS = ["season_type", "game_type", "type"]
TEAM_KEYS = ["recent_team", "team", "posteam"]
OPP_KEYS = ["opponent_team", "opponent", "defteam"]
COLLEGE_KEYS = ["college_name", "college", "school", "school_name", "collegeName"]
META_NAME_KEYS = ["display_name", "player_name", "full_name", "player_display_name", "name"]
META_ID_KEYS = ["gsis_id", "player_id", "nfl_id", "pfr_id", "espn_id"]
FIRST_SEASON_KEYS = ["rookie_year", "first_season", "rookie_season", "entry_year"]
LAST_SEASON_KEYS = ["last_season", "final_season"]


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


def parse_optional_int(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(round(float(text)))
    except (TypeError, ValueError):
        return None


def normalize_player_id(value: str) -> str:
    return str(value or "").strip().upper()


def load_rows(season_start: int, season_end: int, insecure: bool = False) -> list[dict[str, str]]:
    all_rows: list[dict[str, str]] = []
    failed_seasons: list[tuple[int, str]] = []

    for season in range(season_start, season_end + 1):
        loaded_this_season = False
        last_error: Exception | None = None
        for template in SEASON_URL_TEMPLATES:
            for ext in ("csv", "csv.gz"):
                url = template.format(season=season, ext=ext)
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
            if loaded_this_season:
                break
        if not loaded_this_season:
            failed_seasons.append((season, str(last_error or "unknown error")))

    if not all_rows:
        sample = ", ".join(f"{s}: {e}" for s, e in failed_seasons[:3]) if failed_seasons else "no sample errors"
        raise RuntimeError(
            "Could not load any seasonal nflverse stats files. "
            f"Sample season errors -> {sample}"
        )

    if failed_seasons:
        print(f"Warning: skipped {len(failed_seasons)} seasons due to download errors.", file=sys.stderr)
        sample = ", ".join(f"{s}: {e}" for s, e in failed_seasons[:3])
        print(f"Sample season errors -> {sample}", file=sys.stderr)

    return all_rows


def load_player_metadata(insecure: bool = False) -> dict[str, dict[str, dict[str, object]]]:
    rows: list[dict[str, str]] = []
    for url in PLAYER_META_URLS:
        try:
            print(f"Trying {url} ...", file=sys.stderr)
            reader = open_csv_reader(url, insecure=insecure)
            rows = list(reader)
            if rows:
                print(f"Loaded {len(rows)} player metadata rows", file=sys.stderr)
                break
        except (HTTPError, URLError, TimeoutError, OSError, gzip.BadGzipFile):
            continue

    if not rows:
        print("Warning: could not load player metadata; colleges may be missing.", file=sys.stderr)
        return {"by_id": {}, "by_name": {}}

    metadata_by_id: dict[str, dict[str, object]] = {}
    metadata_by_name: dict[str, dict[str, object]] = {}

    def upsert(target: dict[str, dict[str, object]], key: str, college: str, first_season: int | None, last_season: int | None) -> None:
        if key not in target:
            target[key] = {"colleges": set(), "first_season": None, "last_season": None}
        entry = target[key]
        if college:
            entry["colleges"].add(college)
        if first_season is not None and first_season >= 1900:
            if entry["first_season"] is None or first_season < entry["first_season"]:
                entry["first_season"] = first_season
        if last_season is not None and last_season >= 1900:
            if entry["last_season"] is None or last_season > entry["last_season"]:
                entry["last_season"] = last_season

    for row in rows:
        name = first_non_empty(row, META_NAME_KEYS)
        norm_name = normalize_name(name)
        player_id = normalize_player_id(first_non_empty(row, META_ID_KEYS))
        college = first_non_empty(row, COLLEGE_KEYS)
        first_season = parse_optional_int(first_non_empty(row, FIRST_SEASON_KEYS))
        last_season = parse_optional_int(first_non_empty(row, LAST_SEASON_KEYS))

        if player_id:
            upsert(metadata_by_id, player_id, college, first_season, last_season)
        if norm_name:
            upsert(metadata_by_name, norm_name, college, first_season, last_season)

    return {"by_id": metadata_by_id, "by_name": metadata_by_name}


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


def build_player_profiles(rows: list[dict[str, str]], metadata: dict[str, dict[str, dict[str, object]]] | None = None) -> dict[str, object]:
    aggregate: dict[str, dict[str, object]] = {}
    metadata = metadata or {"by_id": {}, "by_name": {}}
    meta_by_id = metadata.get("by_id", {})
    meta_by_name = metadata.get("by_name", {})

    for row in rows:
        season_type = first_non_empty(row, SEASON_TYPE_KEYS, "REG").upper()
        if season_type and season_type not in {"REG", "REGULAR"}:
            continue

        player = first_non_empty(row, NAME_KEYS)
        if not player:
            continue

        player_id = normalize_player_id(first_non_empty(row, PLAYER_ID_KEYS))
        pos = first_non_empty(row, POS_KEYS, "").upper()
        team = first_non_empty(row, TEAM_KEYS, "").upper()
        season = first_non_empty(row, SEASON_KEYS, "")
        season_key = season if season.isdigit() else ""
        norm_name = normalize_name(player)
        aggregate_key = player_id or f"name::{norm_name}"

        if aggregate_key not in aggregate:
            aggregate[aggregate_key] = {
                "player": player,
                "player_id": player_id,
                "norm_name": norm_name,
                "team_counts": Counter(),
                "pos_counts": Counter(),
                "pos_team_counts": defaultdict(Counter),
                "season_team_counts": defaultdict(Counter),
                "season_pos_counts": defaultdict(Counter),
                "season_pos_team_counts": defaultdict(lambda: defaultdict(Counter)),
            }

        entry = aggregate[aggregate_key]
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

    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for _, entry in aggregate.items():
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

        season_years = sorted(int(s) for s in by_season.keys()) if by_season else []
        meta = meta_by_id.get(entry.get("player_id", ""), {}) or meta_by_name.get(entry["norm_name"], {})
        meta_first = parse_optional_int(meta.get("first_season"))
        meta_last = parse_optional_int(meta.get("last_season"))
        start_year = season_years[0] if season_years else None
        end_year = season_years[-1] if season_years else None
        if meta_first is not None and meta_first >= 1900:
            start_year = meta_first if start_year is None else min(start_year, meta_first)
        if meta_last is not None and meta_last >= 1900:
            end_year = meta_last if end_year is None else max(end_year, meta_last)
        colleges = sorted(str(c).strip() for c in meta.get("colleges", set()) if str(c).strip())

        grouped[entry["norm_name"]].append(
            {
                "player": entry["player"],
                "player_id": entry.get("player_id", ""),
                "default": {"team": default_team, "position": default_pos},
                "by_season": by_season,
                "career_start": start_year,
                "career_end": end_year,
                "colleges": colleges,
            }
        )

    profiles: dict[str, object] = {}
    for norm_name, options in grouped.items():
        if len(options) == 1:
            profiles[norm_name] = options[0]
        else:
            options_sorted = sorted(
                options,
                key=lambda x: (
                    -(len(x.get("by_season", {}) or {})),
                    -(x.get("career_end") or 0),
                    x.get("player_id", ""),
                ),
            )
            profiles[norm_name] = options_sorted
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
    profiles_without_colleges = 0
    profiles_without_career_range = 0
    for profile_val in profiles.values():
        profile_options = profile_val if isinstance(profile_val, list) else [profile_val]
        for profile in profile_options:
            default = profile.get("default", {}) if isinstance(profile, dict) else {}
            team = str(default.get("team", "")).upper()
            pos = str(default.get("position", "")).upper()
            by_season_obj = profile.get("by_season", {}) if isinstance(profile, dict) else {}
            colleges = profile.get("colleges", []) if isinstance(profile, dict) else []
            career_start = profile.get("career_start") if isinstance(profile, dict) else None
            career_end = profile.get("career_end") if isinstance(profile, dict) else None
            if team in {"", "UNK", "—"} or pos in {"", "UNK", "—"}:
                profiles_without_default += 1
            if not isinstance(by_season_obj, dict) or not by_season_obj:
                profiles_without_seasons += 1
            if not isinstance(colleges, list) or not colleges:
                profiles_without_colleges += 1
            if not isinstance(career_start, int) or not isinstance(career_end, int):
                profiles_without_career_range += 1

    issues = {
        "missing_team": missing_team,
        "missing_position": missing_position,
        "missing_required_stats": missing_stats,
        "missing_season": missing_season,
        "duplicate_puzzle_keys": len(duplicate_keys),
        "profiles_without_default_team_or_position": profiles_without_default,
        "profiles_without_season_history": profiles_without_seasons,
        "profiles_without_colleges": profiles_without_colleges,
        "profiles_without_career_range": profiles_without_career_range,
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
        "--profile-season-start",
        type=int,
        default=1999,
        help="First season to include for player profile history (default: 1999)",
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
    if args.season_end < args.profile_season_start:
        print("--season-end must be >= --profile-season-start", file=sys.stderr)
        return 1

    puzzle_rows = load_rows(args.season_start, args.season_end, insecure=args.insecure)
    profile_rows = (
        puzzle_rows
        if args.profile_season_start == args.season_start
        else load_rows(args.profile_season_start, args.season_end, insecure=args.insecure)
    )
    metadata = load_player_metadata(insecure=args.insecure)

    puzzles = build_puzzles(puzzle_rows, min_ppr=args.min_ppr, positions=positions)

    if not puzzles:
        print("No puzzles generated. Try lowering --min-ppr or expanding --positions.", file=sys.stderr)
        return 1

    out_path = args.out
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(puzzles, f, indent=2)

    profiles = build_player_profiles(profile_rows, metadata=metadata)
    profiles_out_path = args.profiles_out
    os.makedirs(os.path.dirname(profiles_out_path) or ".", exist_ok=True)
    with open(profiles_out_path, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2)

    search_players = build_search_players(puzzle_rows, positions={"WR", "RB", "TE"})
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
