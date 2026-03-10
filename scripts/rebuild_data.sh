#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

python3 scripts/build_puzzles_from_nflverse.py \
  --insecure \
  --min-ppr 30 \
  --positions WR,RB,TE \
  --season-start 2010 \
  --season-end "$(( $(date +%Y) - 1 ))" \
  --out data/puzzles.json \
  --profiles-out data/player_profiles.json \
  --search-out data/player_search.json \
  --qa-report-out data/qa_report.json \
  --strict-qa

python3 - <<'PY'
import json
from pathlib import Path

base = Path("data")
files = [
    ("puzzles.json", "puzzles_data.js", "__FSL_PUZZLES__"),
    ("player_profiles.json", "player_profiles_data.js", "__FSL_PLAYER_PROFILES__"),
    ("player_search.json", "player_search_data.js", "__FSL_PLAYER_SEARCH__"),
]

for src, dst, var in files:
    data = json.load(open(base / src))
    with open(base / dst, "w", encoding="utf-8") as f:
        f.write(f"window.{var} = ")
        json.dump(data, f, separators=(",", ":"))
        f.write(";\n")
    print(f"Wrote {dst}")
PY
