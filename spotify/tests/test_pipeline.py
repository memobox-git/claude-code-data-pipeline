"""The built layers hold the agreed numbers and every block check passed."""
import pytest

from spotify.common import admin_connect

from .conftest import needs_db

pytestmark = needs_db


@pytest.fixture(scope="module")
def db():
    with admin_connect() as conn:
        yield conn


def one(db, query):
    return db.execute(query).fetchone()[0]


def test_one_row_per_track_and_bad_rows_quarantined(db):
    assert one(db, "SELECT count(*) FROM gold.dim_track") == one(db, "SELECT count(DISTINCT track_id) FROM silver.tracks")
    batch = one(db, "SELECT max(batch_id) FROM gold.dim_track")
    quarantined = one(db, f"SELECT count(*) FROM silver.quarantine WHERE batch_id = '{batch}'")
    pairs = one(db, "SELECT coalesce(sum(source_rows), 0) FROM silver.track_genres")
    assert quarantined + pairs == one(db, f"SELECT count(*) FROM bronze.tracks WHERE batch_id = '{batch}'")


def test_every_bridge_ends_at_the_agreed_number(db):
    tracks = one(db, "SELECT count(*) FROM gold.dim_track")
    rows = db.execute("SELECT DISTINCT ON (team) team, running_total FROM gold.catalog_bridge "
                      "ORDER BY team, step_no DESC").fetchall()
    assert {team for team, _ in rows} == {"Label", "Editorial", "Product"}
    assert all(total == tracks for _, total in rows)
    for team, in db.execute("SELECT DISTINCT team FROM gold.catalog_bridge").fetchall():
        steps = db.execute("SELECT amount FROM gold.catalog_bridge WHERE team = %s", (team,)).fetchall()
        assert sum(a for a, in steps) == tracks


def test_known_numbers_for_the_kaggle_snapshot(db):
    """The figures quoted in the README for maharshipandya/-spotify-tracks-dataset."""
    if one(db, "SELECT amount FROM gold.catalog_bridge WHERE team = 'Label' AND step_no = 1") != 114000:
        pytest.skip("a different file was loaded")
    assert one(db, "SELECT count(*) FROM gold.dim_track") == 89740
    assert one(db, "SELECT count(*) FROM gold.dim_song") == 81206
    assert one(db, "SELECT count(*) FROM gold.dim_genre") == 114


def test_latest_run_of_every_block_check_passed(db):
    batch = one(db, "SELECT max(batch_id) FROM gold.dim_track")
    failed = db.execute(
        "SELECT DISTINCT ON (layer, check_name) layer, check_name, failing_rows FROM meta.dq_results "
        "WHERE batch_id = %s AND severity = 'block' ORDER BY layer, check_name, run_at DESC", (batch,)).fetchall()
    assert failed and all(rows == 0 for _, _, rows in failed), failed


def test_lineage_recorded_for_every_layer(db):
    batch = one(db, "SELECT max(batch_id) FROM gold.dim_track")
    layers = {r[0] for r in db.execute("SELECT DISTINCT layer FROM meta.lineage WHERE batch_id = %s", (batch,))}
    assert layers == {"bronze", "silver", "gold"}
