"""Project store: star picks (save / load / replace / validation) — tmp_path, no network.

Every test runs against a ``tmp_path`` project root. Round-trips close and
reopen the project to prove persistence (db + the ``project.json`` mirror).
"""

import json
import subprocess
import sys

import pytest

from seestar_controller.store import (
    ROLE_COMP,
    ROLE_TARGET,
    ROLES,
    STAR_PICK_FIELDS,
    ProjectError,
    StarPick,
    StarPickError,
    create_project,
    load_star_picks,
    normalize_role,
    open_project,
    save_star_picks,
)


@pytest.fixture()
def root(tmp_path):
    """A fresh project root."""
    return tmp_path / "projects"


@pytest.fixture()
def project(root):
    """One created project, closed after the test."""
    p = create_project("M 42", root=root)
    yield p
    p.close()


def make_pick(
    role=ROLE_TARGET,
    label="TIC 123",
    x=100.0,
    y=200.0,
    ra_deg=83.82208,
    dec_deg=-5.39114,
    aperture_radius=4.0,
    sky_annulus_inner=8.0,
    sky_annulus_outer=12.0,
):
    """A valid StarPick with overridable fields."""
    return StarPick(
        role=role,
        label=label,
        x=x,
        y=y,
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        aperture_radius=aperture_radius,
        sky_annulus_inner=sky_annulus_inner,
        sky_annulus_outer=sky_annulus_outer,
    )


def comp(label, x=300.0, y=400.0, **kwargs):
    """A valid comp pick."""
    return make_pick(role=ROLE_COMP, label=label, x=x, y=y, **kwargs)


TARGET = make_pick()
COMP_A = comp("comp A", x=301.0, y=401.0)
COMP_B = comp("comp B", x=302.0, y=402.0)
COMP_C = comp("comp C", x=303.0, y=403.0)


# --- save -> load round-trip ------------------------------------------------------


def test_save_returns_picks_and_load_roundtrips(project):
    picks = [TARGET, COMP_A, COMP_B]
    returned = save_star_picks(project, picks)
    assert returned == picks
    assert isinstance(returned, list)
    loaded = load_star_picks(project)
    assert loaded == picks
    assert all(isinstance(p, StarPick) for p in loaded)


def test_save_writes_db_rows_with_all_columns(project):
    save_star_picks(project, [TARGET, COMP_A])
    rows = project.db.execute(
        "SELECT role, label, x, y, ra_deg, dec_deg, aperture_radius, "
        "sky_annulus_inner, sky_annulus_outer FROM stars ORDER BY id"
    ).fetchall()
    assert rows == [
        ("target", "TIC 123", 100.0, 200.0, 83.82208, -5.39114, 4.0, 8.0, 12.0),
        ("comp", "comp A", 301.0, 401.0, 83.82208, -5.39114, 4.0, 8.0, 12.0),
    ]
    assert project.db.execute("SELECT COUNT(*) FROM stars").fetchone()[0] == 2


def test_save_mirrors_into_project_json(project):
    save_star_picks(project, [TARGET, COMP_A, COMP_B])
    # in-memory config and the file on disk both carry the mirror
    mirror = {
        "target": TARGET.to_dict(),
        "comps": [COMP_A.to_dict(), COMP_B.to_dict()],
    }
    assert project.config["star_picks"] == mirror
    on_disk = json.loads(project.config_path.read_text(encoding="utf-8"))
    assert on_disk["star_picks"] == mirror
    # mirror shape: exactly the spec placeholder keys, all pick fields present
    assert set(on_disk["star_picks"]) == {"target", "comps"}
    assert set(on_disk["star_picks"]["target"]) == set(STAR_PICK_FIELDS)


def test_roundtrip_across_reopen(root, project):
    picks = [TARGET, COMP_A]
    save_star_picks(project, picks)
    project.close()

    with open_project(root / "m-42") as p:
        assert load_star_picks(p) == picks
        # the mirror survived the reopen too
        assert p.config["star_picks"] == {
            "target": TARGET.to_dict(),
            "comps": [COMP_A.to_dict()],
        }
        # and saving again on the reopened project works
        save_star_picks(p, [TARGET])
        assert load_star_picks(p) == [TARGET]


def test_pixel_only_pick_roundtrips_with_null_ra_dec(root, project):
    pixel_only = make_pick(ra_deg=None, dec_deg=None)
    save_star_picks(project, [pixel_only, COMP_A])
    project.close()
    with open_project(root / "m-42") as p:
        loaded = load_star_picks(p)
        assert loaded[0].ra_deg is None
        assert loaded[0].dec_deg is None
        assert loaded[1].ra_deg == 83.82208
        row = p.db.execute(
            "SELECT ra_deg, dec_deg FROM stars WHERE role = 'target'"
        ).fetchone()
        assert row == (None, None)


# --- replace semantics --------------------------------------------------------------


def test_replace_overwrites_cleanly(project):
    save_star_picks(project, [TARGET, COMP_A, COMP_B])
    new_target = make_pick(label="TIC 999", x=111.0, y=222.0)
    save_star_picks(project, [new_target, COMP_C])

    loaded = load_star_picks(project)
    assert loaded == [new_target, COMP_C]  # old picks are gone
    assert project.db.execute("SELECT COUNT(*) FROM stars").fetchone()[0] == 2
    labels = [r[0] for r in project.db.execute("SELECT label FROM stars")]
    assert "comp A" not in labels and "comp B" not in labels and "TIC 123" not in labels
    # the mirror was replaced too
    assert project.config["star_picks"] == {
        "target": new_target.to_dict(),
        "comps": [COMP_C.to_dict()],
    }


def test_empty_picks_clears_everything(project):
    save_star_picks(project, [TARGET, COMP_A])
    returned = save_star_picks(project, [])
    assert returned == []
    assert load_star_picks(project) == []
    assert project.db.execute("SELECT COUNT(*) FROM stars").fetchone()[0] == 0
    assert project.config["star_picks"] == {"target": None, "comps": []}
    on_disk = json.loads(project.config_path.read_text(encoding="utf-8"))
    assert on_disk["star_picks"] == {"target": None, "comps": []}


def test_empty_picks_on_fresh_project(project):
    assert load_star_picks(project) == []
    save_star_picks(project, [])
    assert load_star_picks(project) == []
    assert project.config["star_picks"] == {"target": None, "comps": []}


def test_single_target_without_comps_is_allowed(project):
    save_star_picks(project, [TARGET])
    assert load_star_picks(project) == [TARGET]
    assert project.config["star_picks"]["comps"] == []


def test_save_accepts_any_iterable(project):
    save_star_picks(project, (TARGET, COMP_A))  # tuple
    assert load_star_picks(project) == [TARGET, COMP_A]
    save_star_picks(project, (p for p in [TARGET, COMP_B]))  # generator
    assert load_star_picks(project) == [TARGET, COMP_B]


def test_save_rejects_non_starpick_items(project):
    with pytest.raises(TypeError, match="StarPick"):
        save_star_picks(project, [TARGET, "comp A"])
    assert load_star_picks(project) == []
    assert project.db.execute("SELECT COUNT(*) FROM stars").fetchone()[0] == 0


# --- exactly-one-target validation ----------------------------------------------------


def test_two_targets_rejected_and_nothing_written(project):
    with pytest.raises(StarPickError) as excinfo:
        save_star_picks(project, [TARGET, make_pick(label="other target")])
    assert "exactly one" in str(excinfo.value)
    assert "2" in str(excinfo.value)
    # nothing was written: db empty, mirror untouched
    assert load_star_picks(project) == []
    assert project.db.execute("SELECT COUNT(*) FROM stars").fetchone()[0] == 0
    assert project.config["star_picks"] is None


def test_comps_only_rejected(project):
    with pytest.raises(StarPickError, match="exactly one"):
        save_star_picks(project, [COMP_A, COMP_B])
    assert load_star_picks(project) == []
    assert project.config["star_picks"] is None


def test_invalid_save_leaves_previous_picks_untouched(project):
    save_star_picks(project, [TARGET, COMP_A])
    mirror_before = dict(project.config["star_picks"])
    with pytest.raises(StarPickError):
        save_star_picks(project, [TARGET, make_pick(), COMP_B])  # two targets
    assert load_star_picks(project) == [TARGET, COMP_A]
    assert project.config["star_picks"] == mirror_before


def test_db_failure_rolls_back_the_replace(project):
    """A failing insert aborts the whole transaction: old picks survive."""
    save_star_picks(project, [TARGET, COMP_A])
    project.db.execute(
        "CREATE TRIGGER no_star_inserts BEFORE INSERT ON stars "
        "BEGIN SELECT RAISE(ABORT, 'no inserts'); END"
    )
    project.db.commit()
    with pytest.raises(StarPickError) as excinfo:
        save_star_picks(project, [make_pick(label="TIC 999"), COMP_B])
    assert "could not save star picks" in str(excinfo.value)
    assert load_star_picks(project) == [TARGET, COMP_A]  # DELETE rolled back
    project.db.execute("DROP TRIGGER no_star_inserts")
    project.db.commit()


# --- ordering ---------------------------------------------------------------------------


def test_load_orders_target_first_then_comps_by_label(project):
    # saved in scrambled order: comps first, target last
    save_star_picks(project, [COMP_C, COMP_A, TARGET, COMP_B])
    loaded = load_star_picks(project)
    assert [p.role for p in loaded] == ["target", "comp", "comp", "comp"]
    assert [p.label for p in loaded] == ["TIC 123", "comp A", "comp B", "comp C"]


def test_load_orders_unnamed_comps_last_and_ties_by_insertion(project):
    unnamed = comp(None, x=310.0, y=410.0)
    dup1 = comp("comp A", x=320.0, y=420.0)
    dup2 = comp("comp A", x=330.0, y=430.0)
    save_star_picks(project, [unnamed, dup1, TARGET, dup2])
    loaded = load_star_picks(project)
    assert [p.label for p in loaded] == ["TIC 123", "comp A", "comp A", None]
    assert [p.x for p in loaded] == [100.0, 320.0, 330.0, 310.0]  # ties by row id


# --- StarPick field validation ------------------------------------------------------------


def test_star_pick_error_is_project_error_and_value_error():
    assert issubclass(StarPickError, ProjectError)
    assert issubclass(StarPickError, ValueError)


@pytest.mark.parametrize("bad_role", ["bogus", "", "TARGET", None, 42, ["target"]])
def test_role_must_be_target_or_comp(bad_role):
    with pytest.raises(StarPickError, match="role"):
        make_pick(role=bad_role)
    with pytest.raises(StarPickError):
        normalize_role(bad_role)


def test_normalize_role_accepts_valid_roles():
    assert normalize_role(ROLE_TARGET) == "target"
    assert normalize_role(ROLE_COMP) == "comp"
    assert ROLES == ("target", "comp")


@pytest.mark.parametrize("bad_label", [42, 3.14, ["comp A"], {"a": 1}])
def test_label_must_be_string_or_none(bad_label):
    with pytest.raises(StarPickError, match="label"):
        make_pick(label=bad_label)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "abc", None, True, [1.0]])
def test_pixel_coords_must_be_finite_numbers(bad):
    with pytest.raises(StarPickError, match="x must be"):
        make_pick(x=bad)
    with pytest.raises(StarPickError, match="y must be"):
        make_pick(y=bad)


@pytest.mark.parametrize("bad", [0.0, -1.0, -0.5, float("nan"), float("inf"), "x", False])
def test_aperture_radius_must_be_positive(bad):
    with pytest.raises(StarPickError, match="aperture_radius"):
        make_pick(aperture_radius=bad)


@pytest.mark.parametrize(
    "inner, outer",
    [(0.0, 12.0), (8.0, 0.0), (-1.0, 12.0), (8.0, -3.0), (12.0, 12.0), (13.0, 12.0), (float("nan"), 12.0)],
)
def test_annulus_must_be_positive_with_inner_lt_outer(inner, outer):
    with pytest.raises(StarPickError, match="annulus"):
        make_pick(sky_annulus_inner=inner, sky_annulus_outer=outer)


def test_annulus_inner_equal_outer_message_names_both():
    with pytest.raises(StarPickError) as excinfo:
        make_pick(sky_annulus_inner=10.0, sky_annulus_outer=10.0)
    assert "10.0" in str(excinfo.value)


@pytest.mark.parametrize("bad_ra", [-1.0, 360.0, 361.5, float("nan"), float("inf")])
def test_ra_deg_range(bad_ra):
    with pytest.raises(StarPickError, match="ra_deg"):
        make_pick(ra_deg=bad_ra)


@pytest.mark.parametrize("bad_dec", [-90.5, 91.0, float("nan"), float("inf")])
def test_dec_deg_range(bad_dec):
    with pytest.raises(StarPickError, match="dec_deg"):
        make_pick(dec_deg=bad_dec)


@pytest.mark.parametrize("ra, dec", [(0.0, -90.0), (359.999, 90.0), (83.82208, -5.39114), (None, None)])
def test_ra_dec_valid_values_accepted(ra, dec):
    pick = make_pick(ra_deg=ra, dec_deg=dec)
    assert pick.ra_deg == ra
    assert pick.dec_deg == dec


def test_numeric_fields_are_coerced_to_float():
    pick = make_pick(x=100, y=200, aperture_radius=4, sky_annulus_inner=8, sky_annulus_outer=12)
    assert pick.x == 100.0 and isinstance(pick.x, float)
    assert pick.y == 200.0 and isinstance(pick.y, float)
    assert pick.aperture_radius == 4.0 and isinstance(pick.aperture_radius, float)
    assert pick.sky_annulus_inner == 8.0
    assert pick.sky_annulus_outer == 12.0


# --- to_dict / from_dict -------------------------------------------------------------------


def test_to_dict_from_dict_roundtrip():
    pick = make_pick()
    d = pick.to_dict()
    assert d == {
        "role": "target",
        "label": "TIC 123",
        "x": 100.0,
        "y": 200.0,
        "ra_deg": 83.82208,
        "dec_deg": -5.39114,
        "aperture_radius": 4.0,
        "sky_annulus_inner": 8.0,
        "sky_annulus_outer": 12.0,
    }
    assert list(d) == list(STAR_PICK_FIELDS)
    assert StarPick.from_dict(d) == pick
    assert json.loads(json.dumps(d)) == d  # JSON-serializable (the mirror)


def test_from_dict_ignores_extra_keys_and_rejects_missing():
    d = make_pick().to_dict()
    d["future_field"] = 1
    assert StarPick.from_dict(d) == make_pick()
    for key in STAR_PICK_FIELDS:
        broken = {k: v for k, v in d.items() if k != key}
        with pytest.raises(StarPickError, match="missing keys"):
            StarPick.from_dict(broken)
    with pytest.raises(StarPickError, match="mapping"):
        StarPick.from_dict(["not", "a", "mapping"])


# --- db is authoritative over the project.json mirror -----------------------------------------


def test_load_reads_db_not_the_mirror(root, project):
    save_star_picks(project, [TARGET, COMP_A])
    # tamper with the mirror only: load must still return the db picks
    project.config["star_picks"] = {"tampered": True}
    project.save_config()
    assert load_star_picks(project) == [TARGET, COMP_A]
    project.close()
    with open_project(root / "m-42") as p:
        assert p.config["star_picks"] == {"tampered": True}  # mirror as written
        assert load_star_picks(p) == [TARGET, COMP_A]  # db wins


# --- public API ---------------------------------------------------------------------------------


def test_star_picks_api_is_exported_from_package():
    import seestar_controller.store as store

    for name in (
        "StarPick",
        "StarPickError",
        "ROLE_TARGET",
        "ROLE_COMP",
        "ROLES",
        "STAR_PICK_FIELDS",
        "save_star_picks",
        "load_star_picks",
        "normalize_role",
    ):
        assert name in store.__all__, name
        assert hasattr(store, name), name
    assert issubclass(store.StarPickError, store.ProjectError)
    assert issubclass(store.StarPickError, ValueError)
    assert callable(store.save_star_picks)
    assert callable(store.load_star_picks)
    assert callable(store.normalize_role)
    assert isinstance(store.StarPick, type)
    assert store.ROLE_TARGET == "target" and store.ROLE_COMP == "comp"


def test_stars_module_imports_no_gtk_and_no_link():
    import subprocess
    import sys

    code = (
        "import sys;"
        "import seestar_controller.store.stars;"
        "assert 'gi' not in sys.modules, 'GTK imported';"
        "assert not any(m == 'seestar_controller.link' or m.startswith("
        "'seestar_controller.link.') for m in sys.modules), 'link imported';"
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
