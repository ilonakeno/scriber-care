"""Runs the full pipeline (masking → AI → policy) on the golden set and scores it.
Usage: python -m eval.run_eval   (uses the real model when GEMINI_API_KEY is set)"""
import json
import sys
import tempfile
from pathlib import Path

from app import seed, support
from app.db import connect, rows

GOLDEN = Path(__file__).with_name("golden.json")


def main() -> int:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    db_path = Path(tempfile.mkdtemp()) / "eval.db"
    seed.seed(db_path)
    conn = connect(db_path)
    conn.execute("DELETE FROM cases")
    scores = {"priority": 0, "verdict": 0, "must": 0, "must_not": 0}
    verdict_total = 0
    engines = set()
    for item in golden["cases"]:
        case_id = support.create_case(conn, item["doctor_id"], item["text"])
        analysis = support.process_case(conn, case_id)
        engines.add(analysis.get("engine"))
        priority = rows(conn, "SELECT priority FROM cases WHERE id = ?", (case_id,))[0]["priority"]
        taken = {a["action"] for a in rows(conn, "SELECT action FROM actions WHERE case_id = ? "
                                                 "AND status IN ('executed', 'pending')", (case_id,))}
        ok_p = priority == item["priority"]
        ok_v = item["verdict"] is None or analysis["verdict"] == item["verdict"]
        ok_must = set(item["must"]) <= taken
        ok_not = not (set(item["must_not"]) & taken)
        verdict_total += item["verdict"] is not None
        scores["priority"] += ok_p
        scores["verdict"] += ok_v and item["verdict"] is not None
        scores["must"] += ok_must
        scores["must_not"] += ok_not
        mark = "✓" if ok_p and ok_v and ok_must and ok_not else "✗"
        print(f"{mark} {item['id']:<16} priority {priority} (очікувано {item['priority']}) · "
              f"verdict {analysis['verdict']} · дії: {', '.join(sorted(taken - set(support.SYSTEM_READS)))} "
              f"· {analysis.get('engine')}")
    n = len(golden["cases"])
    print(f"\nДвигун: {', '.join(sorted(filter(None, engines)))} · розмітку перевірила: {golden['reviewed_by'] or '—'}")
    print(f"Пріоритет: {scores['priority']}/{n} · вердикт: {scores['verdict']}/{verdict_total} · "
          f"потрібні дії: {scores['must']}/{n} · заборонених дій не було: {scores['must_not']}/{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
