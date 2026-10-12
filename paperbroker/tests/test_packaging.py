"""B-13: the package metadata must match what the server actually needs.

Measured before the fix (2026-10-12, fresh venv): ``pip install .`` installed
arrow and requests but no Flask (``import flask`` -> ModuleNotFoundError), and
the wheel carried no ``templates/`` directory, so a fresh installation answered
``GET /`` with 500 ``TemplateNotFound: screen.html``. Docker was never affected
- the image installs requirements.txt and copies the whole package directory.

Both pins close the CLASS, not the single symptom: every runtime requirement in
requirements.txt must be declared in setup.py, and every non-Python file that
belongs to a shipped package must be covered by a ``package_data`` pattern. A
later asset (css, js, json) is therefore covered automatically.

Precondition of T1: requirements.txt lists RUNTIME dependencies only (no dev or
test tooling). That holds today; if tooling is ever added there, T1 must be
scoped instead of relaxed.
"""
import ast
import fnmatch
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _setup_kwargs():
    """Read setup(...)'s keyword arguments from the file.

    ast, not import: importing setup.py would need setuptools in the test env.
    """
    tree = ast.parse((ROOT / "setup.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            if "install_requires" in kwargs or "package_data" in kwargs:
                return {k: ast.literal_eval(v) for k, v in kwargs.items()}
    raise AssertionError("no setup(...) call with install_requires/package_data found")


def _names(entries):
    """``arrow>=0.10.0`` -> ``arrow``; ``flask>=3.1`` -> ``flask``.

    Extras and environment markers are not used in this project (measured
    2026-10-12); if they are added, this helper must handle them.
    """
    out = set()
    for entry in entries:
        name = re.split(r"[<>=!~\[; ]", entry.strip(), maxsplit=1)[0]
        out.add(name.strip().lower().replace("_", "-"))
    return out


def test_requirements_txt_and_install_requires_agree():
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    wanted = _names([line for line in lines
                     if line.strip() and not line.strip().startswith("#")])
    declared = _names(_setup_kwargs()["install_requires"])
    missing = sorted(wanted - declared)
    assert missing == [], f"declared in requirements.txt but not in setup.py: {missing}"


def test_every_shipped_asset_is_covered_by_package_data():
    kwargs = _setup_kwargs()
    patterns = [pat for globs in kwargs.get("package_data", {}).values() for pat in globs]
    shipped = []
    for pkg in kwargs["packages"]:
        pkg_dir = ROOT / pkg.replace(".", "/")
        for f in sorted(pkg_dir.rglob("*")):
            if not f.is_file() or f.suffix == ".py" or "__pycache__" in f.parts:
                continue
            rel = f.relative_to(pkg_dir).as_posix()
            if rel.startswith("tests/"):
                # measured: paperbroker.tests has an __init__.py but is NOT in
                # `packages`, so it does not ship - its fixture data.csv.gz is
                # out of scope for a shipping invariant.
                continue
            shipped.append(rel)
    assert shipped, "no assets found - the measurement itself is broken"
    uncovered = [rel for rel in shipped
                 if not any(fnmatch.fnmatch(rel, pat) for pat in patterns)]
    assert uncovered == [], f"shipped but not covered by package_data: {uncovered}"
