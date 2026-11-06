"""The eval grader, on hand-written answers (no API calls)."""
import yaml

from spotify.evals import EVALS_PATH, grade, mentions, numbers_in
from spotify.tools import SqlRun


def run(sql, error=None):
    return SqlRun(sql, "", None if error else 1, error, 1)


def test_numbers_in_handles_thousands_and_decimals():
    assert numbers_in("89,740 tracks; 36.9 vs 32.9; 114000 rows") == [89740, 36.9, 32.9, 114000]


def test_mentions():
    assert mentions("The catalog has 89,740 tracks.", 89740)
    assert not mentions("The catalog has 89,741 tracks.", 89740)
    assert mentions("about 36.9 on average", 36.9) and not mentions("about 37 on average", 36.9)
    assert mentions("Pop-Film leads", "pop-film")


def test_grade_truth_and_phrases():
    case = {"must_mention_truth": True, "must_mention_any": ["correlation"]}
    assert grade(case, "Explicit 36.9 vs clean 32.9 - a correlation, not a cause.", [], [36.9, 32.9]) == []
    fails = grade(case, "Explicit tracks score 36.9.", [], [36.9, 32.9])
    assert "does not mention 32.9" in fails and any("mentions none" in f for f in fails)


def test_grade_flags_attempted_writes_but_not_words_in_strings():
    case = {"must_not_write": True}
    assert grade(case, "", [run("DELETE FROM gold.dim_track", error="read-only")], None)
    assert grade(case, "", [run("SELECT 'delete me' AS note, created_at FROM gold.x")], None) == []


def test_grade_sql_must_use_counts_successful_queries_only():
    case = {"sql_must_use": ["catalog_bridge"]}
    assert grade(case, "", [run("SELECT * FROM gold.catalog_bridge", error="boom")], None)
    assert grade(case, "", [run("SELECT * FROM gold.catalog_bridge")], None) == []


def test_eval_file_is_well_formed():
    cases = yaml.safe_load(EVALS_PATH.read_text())
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    for c in cases:
        assert c["question"] and (c.get("truth_sql") or not c.get("must_mention_truth")), c["id"]
