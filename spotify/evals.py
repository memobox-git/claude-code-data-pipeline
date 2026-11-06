"""Golden-question evals for the data agent.

    python -m spotify.evals                    # every case in evals.yaml
    python -m spotify.evals explicit_gap       # one or more cases by id
    python -m spotify.evals --trials 3         # repeat each case to catch flaky answers

Each case gets a fresh agent (no shared history). Grading is deterministic; expected values
come from truth_sql run against gold now. Results go to data/evals/<timestamp>.jsonl.
This spends real API calls: roughly 3-6 model calls per case.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import yaml

from .common import PKG, REPO
from .tools import GoldTools, SqlRun, strip_noise

EVALS_PATH = PKG / "evals.yaml"
_NUM = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")
_WRITE = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create|grant|copy)\b", re.I)


def numbers_in(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in _NUM.findall(text)]


def mentions(text: str, value: Any) -> bool:
    """Integers must appear exactly, decimals within 0.05, text case-insensitively."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return str(value).lower() in text.lower()
    tolerance = 0.5 if isinstance(value, int) else 0.05
    return any(abs(n - float(value)) < tolerance for n in numbers_in(text))


def grade(case: dict, text: str, runs: list[SqlRun], truth: list | None) -> list[str]:
    """Return the reasons the answer fails (empty list = pass)."""
    fails = []
    if case.get("must_mention_truth"):
        fails += [f"does not mention {v!r}" for v in truth or [] if not mentions(text, v)]
    phrases = case.get("must_mention_any")
    if phrases and not any(p.lower() in text.lower() for p in phrases):
        fails.append(f"mentions none of {phrases}")
    if case.get("must_not_write"):
        tried = [r.sql for r in runs if _WRITE.search(strip_noise(r.sql))]
        if tried:
            fails.append(f"tried to write: {tried[0][:80]}")
    ran = " ".join(r.sql for r in runs if not r.error).lower()
    fails += [f"no successful SQL used {t}" for t in case.get("sql_must_use", []) if t.lower() not in ran]
    return fails


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="case ids to run (default: all)")
    ap.add_argument("--trials", type=int, default=1)
    a = ap.parse_args()

    from .agent import DataAgent  # imported here so grading can be tested without the SDK client

    cases = [c for c in yaml.safe_load(EVALS_PATH.read_text()) if not a.ids or c["id"] in a.ids]
    if not cases:
        sys.exit(f"no cases match {a.ids}")
    truth_tools = GoldTools()
    out_path = REPO / "data" / "evals" / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    passed = total = 0
    tokens = {"in": 0, "out": 0}
    print(f"{'case':22} {'trial':>5}  {'result':6} {'steps':>5} {'sql':>4}  why")
    for case in cases:
        truth = json.loads(truth_tools.run_sql(case["truth_sql"]))["rows"][0] if case.get("truth_sql") else None
        for trial in range(1, a.trials + 1):
            agent = DataAgent()
            try:
                answer = agent.ask(case["question"])
            finally:
                agent.tools.close()
            fails = grade(case, answer.text, answer.sql_runs, truth)
            ok = not fails and answer.stop_reason == "end_turn"
            if answer.stop_reason != "end_turn":
                fails.append(f"stopped: {answer.stop_reason}")
            passed += ok
            total += 1
            tokens["in"] += answer.usage.get("input_tokens", 0) + answer.usage.get("cache_read_input_tokens", 0)
            tokens["out"] += answer.usage.get("output_tokens", 0)
            print(f"{case['id']:22} {trial:>5}  {'PASS' if ok else 'FAIL':6} {answer.steps:>5} "
                  f"{len(answer.sql_runs):>4}  {'; '.join(fails)}", flush=True)
            with out_path.open("a") as fh:
                fh.write(json.dumps({"case": case["id"], "trial": trial, "pass": ok, "fails": fails,
                                     "truth": truth, **asdict(answer)}, default=str) + "\n")
    truth_tools.close()
    print(f"\n{passed}/{total} passed; {tokens['in']} input / {tokens['out']} output tokens; "
          f"details in {out_path.relative_to(REPO)}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
