#!/usr/bin/env python3
"""
One-off DSS historic backfill.

Reads scripts/dss/dss_backfill_data.json and inserts shifts + completions
+ landings for the Dubai team. Idempotent: re-running skips existing
(team_member_id, work_date) shifts and existing
(team_id, work_date, task_type_id) landings.

Usage:
    DATABASE_URL=postgresql://... python scripts/dss/backfill.py
"""

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor


DATA_FILE = Path(__file__).resolve().parent / "dss_backfill_data.json"


def get_db_conn():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)


def main():
    if not DATA_FILE.exists():
        print(f"ERROR: data file not found: {DATA_FILE}", file=sys.stderr)
        sys.exit(1)

    with open(DATA_FILE) as f:
        data = json.load(f)

    team_name = data["team"]["name"]
    warnings = []

    conn = get_db_conn()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 1. Find Dubai team
            cur.execute("SELECT id FROM dss_teams WHERE name = %s", (team_name,))
            row = cur.fetchone()
            if not row:
                print(
                    f"ERROR: team '{team_name}' not found. Run the main app once "
                    f"to seed it.",
                    file=sys.stderr,
                )
                sys.exit(1)
            team_id = row["id"]
            print(f"Team '{team_name}' id={team_id}")

            # 2. Resolve / create team members
            member_id_by_name = {}
            for name in data["team_members"]:
                cur.execute(
                    "SELECT id FROM dss_team_members "
                    "WHERE team_id = %s AND name = %s",
                    (team_id, name),
                )
                r = cur.fetchone()
                if r:
                    member_id_by_name[name] = r["id"]
                else:
                    cur.execute(
                        "INSERT INTO dss_team_members (team_id, name, is_active) "
                        "VALUES (%s, %s, TRUE) RETURNING id",
                        (team_id, name),
                    )
                    member_id_by_name[name] = cur.fetchone()["id"]
                    print(f"  + created member: {name}")

            # 3. Resolve / create task types and sub-types
            type_id_by_name = {}
            base_rate = None
            sub_type_id_by_pair = {}
            type_rate_by_name = {}

            for tt in data["task_types"]:
                t_name = tt["name"]
                t_rate = Decimal(str(tt["rate_per_hour"]))
                t_is_base = bool(tt["is_base"])
                t_order = int(tt["display_order"])
                cur.execute(
                    "SELECT id, rate_per_hour, is_base "
                    "FROM dss_task_types WHERE team_id = %s AND name = %s",
                    (team_id, t_name),
                )
                r = cur.fetchone()
                if r:
                    type_id_by_name[t_name] = r["id"]
                    if Decimal(r["rate_per_hour"]) != t_rate:
                        warnings.append(
                            f"task_type '{t_name}' rate differs: "
                            f"DB={r['rate_per_hour']} JSON={t_rate} (kept DB)"
                        )
                    if bool(r["is_base"]) != t_is_base:
                        warnings.append(
                            f"task_type '{t_name}' is_base differs: "
                            f"DB={r['is_base']} JSON={t_is_base} (kept DB)"
                        )
                    type_rate_by_name[t_name] = Decimal(r["rate_per_hour"])
                    if r["is_base"]:
                        base_rate = Decimal(r["rate_per_hour"])
                else:
                    cur.execute(
                        "INSERT INTO dss_task_types "
                        "(team_id, name, rate_per_hour, is_base, display_order, "
                        "is_active) VALUES (%s, %s, %s, %s, %s, TRUE) "
                        "RETURNING id",
                        (team_id, t_name, t_rate, t_is_base, t_order),
                    )
                    type_id_by_name[t_name] = cur.fetchone()["id"]
                    type_rate_by_name[t_name] = t_rate
                    if t_is_base:
                        base_rate = t_rate
                    print(f"  + created task type: {t_name}")

                # Sub-types
                for idx, sub_name in enumerate(tt.get("sub_types") or []):
                    parent_id = type_id_by_name[t_name]
                    cur.execute(
                        "SELECT id FROM dss_task_sub_types "
                        "WHERE task_type_id = %s AND name = %s",
                        (parent_id, sub_name),
                    )
                    r = cur.fetchone()
                    if r:
                        sub_type_id_by_pair[(t_name, sub_name)] = r["id"]
                    else:
                        cur.execute(
                            "INSERT INTO dss_task_sub_types "
                            "(task_type_id, name, display_order, is_active) "
                            "VALUES (%s, %s, %s, TRUE) RETURNING id",
                            (parent_id, sub_name, idx + 1),
                        )
                        sub_type_id_by_pair[(t_name, sub_name)] = (
                            cur.fetchone()["id"]
                        )
                        print(f"  + created sub-type: {t_name}/{sub_name}")

            if base_rate is None:
                print(
                    "ERROR: no base task type found (need is_base=true)",
                    file=sys.stderr,
                )
                sys.exit(1)

            # 4. Insert shifts + completions
            inserted_shifts = 0
            inserted_completions = 0
            skipped_shifts = 0

            for shift in data["shifts"]:
                work_date = shift["work_date"]
                agent_name = shift["agent_name"]
                hours = Decimal(str(shift["hours_worked"]))
                completions = shift.get("completions") or []

                if agent_name not in member_id_by_name:
                    warnings.append(
                        f"unknown agent '{agent_name}' on {work_date} (skipped)"
                    )
                    continue
                member_id = member_id_by_name[agent_name]

                cur.execute(
                    "SELECT id FROM dss_daily_shifts "
                    "WHERE team_member_id = %s AND work_date = %s",
                    (member_id, work_date),
                )
                if cur.fetchone():
                    skipped_shifts += 1
                    continue

                cur.execute(
                    "INSERT INTO dss_daily_shifts "
                    "(team_id, team_member_id, work_date, hours_worked) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (team_id, member_id, work_date, hours),
                )
                shift_id = cur.fetchone()["id"]
                inserted_shifts += 1

                for comp in completions:
                    t_name = comp["task_type"]
                    s_name = comp.get("sub_type")
                    count = int(comp["count"])

                    if t_name not in type_id_by_name:
                        warnings.append(
                            f"unknown task type '{t_name}' on {work_date} "
                            f"for {agent_name} (skipped)"
                        )
                        continue
                    t_id = type_id_by_name[t_name]
                    s_id = None
                    if s_name:
                        key = (t_name, s_name)
                        if key not in sub_type_id_by_pair:
                            warnings.append(
                                f"unknown sub-type '{t_name}/{s_name}' on "
                                f"{work_date} for {agent_name} (skipped)"
                            )
                            continue
                        s_id = sub_type_id_by_pair[key]

                    task_rate = type_rate_by_name[t_name]
                    cf = base_rate / task_rate

                    cur.execute(
                        "INSERT INTO dss_daily_completions "
                        "(daily_shift_id, task_type_id, task_sub_type_id, "
                        "count, conversion_factor) VALUES (%s, %s, %s, %s, %s)",
                        (shift_id, t_id, s_id, count, cf),
                    )
                    inserted_completions += 1

            # 5. Insert landings
            inserted_landings = 0
            skipped_landings = 0
            for landing in data["landings"]:
                work_date = landing["work_date"]
                for item in landing.get("items") or []:
                    t_name = item["task_type"]
                    count = int(item["count"])
                    if t_name not in type_id_by_name:
                        warnings.append(
                            f"unknown task type '{t_name}' in landing on "
                            f"{work_date} (skipped)"
                        )
                        continue
                    t_id = type_id_by_name[t_name]
                    cur.execute(
                        "SELECT id FROM dss_daily_landings "
                        "WHERE team_id = %s AND work_date = %s "
                        "AND task_type_id = %s",
                        (team_id, work_date, t_id),
                    )
                    if cur.fetchone():
                        skipped_landings += 1
                        continue
                    cur.execute(
                        "INSERT INTO dss_daily_landings "
                        "(team_id, work_date, task_type_id, count) "
                        "VALUES (%s, %s, %s, %s)",
                        (team_id, work_date, t_id, count),
                    )
                    inserted_landings += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print()
    print("=" * 60)
    print(f"Inserted {inserted_shifts} shifts, "
          f"{inserted_completions} completions, "
          f"{inserted_landings} landings.")
    print(f"Skipped {skipped_shifts} existing shifts, "
          f"{skipped_landings} existing landings.")
    if warnings:
        print(f"Warnings ({len(warnings)}):")
        for w in warnings:
            print(f"  - {w}")
    else:
        print("Warnings: none")


if __name__ == "__main__":
    main()
