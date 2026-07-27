"""End-to-end proof of the universal ingestion milestone.

Imports the SAME roster encoded in 5 completely different file formats and
verifies every format yields identical daily care-minute numbers. First run
learns each format via the AI analyzer (needs ANTHROPIC_API_KEY); re-runs
reuse the stored mappings with zero AI calls.

Run:  python test_ingestion.py
"""

import json
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "test_data", "test_ingestion.db")
os.environ["DATABASE_PATH"] = DB_PATH

FIXTURES = [
    "format1_simple.csv",
    "format2_humanforce.csv",
    "format3_alayacare.xlsx",
    "format4_payroll_hours.csv",
    "format5_roster.txt",
]
RESIDENTS_FILE = "residents_clients.xlsx"
DAYS = [date(2026, 4, 13), date(2026, 4, 14), date(2026, 4, 15)]


def main():
    fixture_dir = os.path.join(HERE, "test_data")
    if not all(os.path.exists(os.path.join(fixture_dir, f)) for f in FIXTURES + [RESIDENTS_FILE]):
        print("Generating fixtures...")
        import subprocess
        subprocess.run([sys.executable, os.path.join(fixture_dir, "make_fixtures.py")], check=True)

    from app import app
    from care_minutes import daily_stats
    from ingestion.pipeline import ingest_file
    from models import Facility, db

    outcomes = {}
    with app.app_context():
        facility = Facility.query.first()
        if not facility:
            facility = Facility(name="Ingestion Test Facility")
            db.session.add(facility)
            db.session.flush()

        with open(os.path.join(fixture_dir, RESIDENTS_FILE), "rb") as f:
            r = ingest_file(facility, RESIDENTS_FILE, f.read())
        db.session.commit()
        print(f"Residents: {r.residents_imported} imported "
              f"({'reused mapping' if r.mapping_reused else 'new mapping learned'})\n")

        for name in FIXTURES:
            with open(os.path.join(fixture_dir, name), "rb") as f:
                outcome = ingest_file(facility, name, f.read())
            db.session.commit()

            stats = [daily_stats(facility.id, d, 215.0, 44.0) for d in DAYS]
            key = tuple((s["total_minutes"], s["rn_minutes"], s["active_residents"]) for s in stats)
            outcomes[name] = (outcome, key)

            tag = "reused" if outcome.mapping_reused else (
                f"learned ({outcome.ai_usage['calls']} AI call(s), "
                f"{outcome.ai_usage['input_tokens'] + outcome.ai_usage['output_tokens']} tokens)"
            )
            print(f"{name}")
            print(f"  mapping: {tag}")
            print(f"  shifts imported: {outcome.shifts_imported}, rows skipped: {len(outcome.row_errors)}")
            for d, s in zip(DAYS, stats):
                print(f"  {d}: total={s['total_minutes']}m  rn={s['rn_minutes']}m  "
                      f"residents={s['active_residents']}  care/resident={s['care_per_resident']}")
            print()

    keys = {k for _, k in outcomes.values()}
    if len(keys) == 1:
        print("PASS — all 5 formats normalized to identical care-minute numbers.")
    else:
        print("FAIL — formats disagree:")
        for name, (o, key) in outcomes.items():
            print(f"  {name}: {key}")
            print(f"    spec: {json.dumps(o.spec, default=str)[:400]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
