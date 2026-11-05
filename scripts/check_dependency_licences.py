"""Every licence in the tree, resolved, or the check goes red.

A licence inventory written by hand is wrong the day after someone adds a
dependency, and a wrong inventory is worse than none because a reviewer trusts
it. So this reads the licences off the packages that are actually installed and
off the dashboard lockfile, writes docs/DEPENDENCIES.md, and refuses to pass
when a licence cannot be resolved or is not one we have already accepted.

    make licences         # check: fails if the file is stale or a licence is new
    make licences-write   # regenerate the file
    python scripts/check_dependency_licences.py --self-test

Every licence accepted here was read against Apache-2.0, the licence Korely
Agent is distributed under (OUTBOUND): each one lets its package ship inside an
Apache 2.0 distribution without changing what the people who receive it are
allowed to do. The permissive ones ask only that their notices travel, and the
packages carry them; the two copyleft ones are library licences, satisfied by
using the package unmodified. A strong copyleft licence (GPL, AGPL) would not
be, and fails like any licence nobody has read. The check also fails when
pyproject.toml declares a licence other than OUTBOUND, because then every
review below was made against the wrong one.

The self-test exists because a check that has never been seen to fail is not
evidence of anything. It feeds the checker a package with no licence and a
package under a licence nobody approved, and fails if either gets through.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "DEPENDENCIES.md"
LOCK = ROOT / "dashboard" / "package-lock.json"
PKG = ROOT / "dashboard" / "package.json"

REQUIREMENTS = ROOT / "requirements" / "requirements.txt"
PYPROJECT = ROOT / "pyproject.toml"

# The licence Korely Agent ships under, the one every review below was made
# against. pyproject.toml must say the same (`_outbound_declared`).
OUTBOUND = "Apache-2.0"


def _direct_python() -> set[str]:
    """The packages requirements.txt names, lower-cased as `_python_deps` compares.

    Read from the file rather than listed here: a second list is a second place
    to forget, and the day cryptography and httpx were declared it would have
    gone on calling them transitive.
    """
    names = set()
    for line in REQUIREMENTS.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            names.add(_normal(re.split(r"[\s\[<>=!~;]", line, maxsplit=1)[0]))
    return names


def _normal(name: str) -> str:
    """A distribution's name as PEP 503 compares it: `pydantic-core` in
    requirements.txt is the `pydantic_core` its metadata names."""
    return re.sub(r"[-_.]+", "-", name).lower()


DIRECT_PY = _direct_python()

# Names as the packaging ecosystem writes them, mapped to what they mean.
# Anything not in here is unresolved and the check goes red.
SPDX = {
    "MIT License": "MIT",
    "MIT": "MIT",
    "MIT-0": "MIT-0",
    "MIT AND PSF-2.0": "MIT AND PSF-2.0",
    "Apache Software License": "Apache-2.0",
    "Apache-2.0": "Apache-2.0",
    "Apache 2.0": "Apache-2.0",
    "BSD License": "BSD-3-Clause",
    "BSD-3-Clause": "BSD-3-Clause",
    # protobuf's License field (it declares no expression and no classifier);
    # its LICENSE is the 3-clause BSD text.
    "3-Clause BSD License": "BSD-3-Clause",
    # numpy 2.4's own expression: numpy under BSD-3-Clause and the code it
    # vendors under the other four, every one permissive (the licence files
    # in its dist-info, read 2026-09-29: libdivide Zlib, pythoncapi-compat
    # 0BSD, highway's random generator and splitmix64 CC0, others MIT). Its
    # compiled libraries are in the document's own section below.
    "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0":
        "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
    "BSD-2-Clause": "BSD-2-Clause",
    "ISC": "ISC",
    # tslib's (TypeScript's runtime helpers, through firebase): the Zero-Clause
    # BSD, permission to use, copy, modify and distribute with no condition at
    # all, not even keeping the notice.
    "0BSD": "0BSD",
    "PSF-2.0": "PSF-2.0",
    "Python Software Foundation License": "PSF-2.0",
    "Apache-2.0 OR BSD-3-Clause": "Apache-2.0 OR BSD-3-Clause",
    "Apache-2.0 OR BSD-2-Clause": "Apache-2.0 OR BSD-2-Clause",
    "Apache-2.0 OR MIT": "Apache-2.0 OR MIT",
    "MIT OR Apache-2.0": "MIT OR Apache-2.0",
    "MIT License; Apache Software License": "MIT OR Apache-2.0",
    "Apache Software License; MIT License": "Apache-2.0 OR MIT",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "MPL-2.0": "MPL-2.0",
    "GNU Library or Lesser General Public License (LGPL)": "LGPL-3.0-or-later",
    "LGPL-3.0-or-later": "LGPL-3.0-or-later",
}

# Permissive: nothing to do beyond keeping the copyright notice, which the
# packages carry with them inside the image.
PERMISSIVE = {
    "MIT", "MIT-0", "MIT AND PSF-2.0", "Apache-2.0", "BSD-3-Clause",
    "BSD-2-Clause", "ISC", "PSF-2.0", "Apache-2.0 OR BSD-3-Clause",
    "Apache-2.0 OR BSD-2-Clause", "Apache-2.0 OR MIT", "MIT OR Apache-2.0",
    "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0", "0BSD",
}

# Copyleft we have looked at and accepted, each with the condition attached.
# A copyleft licence that is not in this dict fails the check, on purpose: the
# point is that nobody adds one without reading it.
COPYLEFT_REVIEWED = {
    "LGPL-3.0-or-later": (
        "Used as a library, unmodified, dynamically linked. The obligation is "
        "to say so, to ship the licence text (the wheel carries it), and to "
        "leave the user able to replace it. We do all three and we do not "
        "patch the package."
    ),
    "MPL-2.0": (
        "File-level copyleft. It bites only on modified files of that project, "
        "and we modify none. Shipping it unchanged inside the image is the "
        "case the licence was written for."
    ),
}

# Packages whose metadata says nothing, resolved by reading the project's own
# licence file. Each entry is a promise that somebody looked.
RESOLVED_BY_HAND = {
    "setuptools": ("MIT", "upstream LICENSE, https://github.com/pypa/setuptools"),
}

CLASSIFIER = re.compile(r"^License :: (?:OSI Approved :: )?(.+)$")


@dataclass
class Dep:
    name: str
    version: str
    raw: str | None
    spdx: str | None
    direct: bool
    where: str          # "python" | "dashboard"
    source: str         # where the licence claim comes from


def _python_deps(distributions) -> list[Dep]:
    out = []
    for dist in distributions:
        md = dist.metadata
        name = md["Name"]
        if not name:
            continue
        raw, source = _declared(md)
        if raw is None and name in RESOLVED_BY_HAND:
            raw, source = RESOLVED_BY_HAND[name][0], RESOLVED_BY_HAND[name][1]
        out.append(Dep(
            name=name,
            version=dist.version,
            raw=raw,
            spdx=SPDX.get(raw) if raw else None,
            direct=_normal(name) in DIRECT_PY,
            where="python",
            source=source or "",
        ))
    return sorted(out, key=lambda d: d.name.lower())


def _declared(md):
    expr = md.get("License-Expression")
    if expr:
        return expr.strip(), "License-Expression"
    names = [
        CLASSIFIER.match(c).group(1)
        for c in (md.get_all("Classifier") or [])
        if CLASSIFIER.match(c)
    ]
    names = [n for n in names if n != "OSI Approved"]
    if names:
        return "; ".join(names), "classifier"
    lic = (md.get("License") or "").strip()
    if lic and "\n" not in lic and len(lic) < 60:
        return lic, "License field"
    return None, None


def _resolve(packages: dict, name: str, parent: str) -> str | None:
    """The lockfile path npm installs `name` at for the package at `parent`
    ("" for the dashboard itself): its own node_modules first, then each
    enclosing one up to the top, as Node resolves a require. A package
    nested because two versions are needed (firebase's @firebase/auth, 8 Oct
    2026) was reported "no licence declared": only the top was looked at."""
    here = parent
    while True:
        path = f"{here}/node_modules/{name}" if here else f"node_modules/{name}"
        if path in packages:
            return path
        if not here:
            return None
        cut = here.rfind("/node_modules/")
        here = here[:cut] if cut >= 0 else ""


def _dashboard_deps() -> list[Dep]:
    lock = json.loads(LOCK.read_text())
    packages = lock["packages"]
    direct = set(json.loads(PKG.read_text())["dependencies"])
    seen: set[str] = set()
    queue = [(name, "") for name in direct]
    out = []
    while queue:
        name, parent = queue.pop(0)
        path = _resolve(packages, name, parent)
        if path in seen:
            continue
        if path is None:
            if name not in {d.name for d in out}:
                out.append(Dep(name, "?", None, None, name in direct, "dashboard",
                               "missing from lockfile"))
            continue
        seen.add(path)
        entry = packages[path]
        if any(d.name == name and d.version == entry.get("version", "?") for d in out):
            # The same version installed twice, nested under two packages:
            # one row, one licence to read.
            queue += [(dep, path) for dep in (entry.get("dependencies") or {})]
            continue
        raw = entry.get("license")
        out.append(Dep(
            name=name,
            version=entry.get("version", "?"),
            raw=raw,
            spdx=SPDX.get(raw) if raw else None,
            direct=name in direct,
            where="dashboard",
            source="package-lock.json",
        ))
        queue += [(dep, path) for dep in (entry.get("dependencies") or {})]
    return sorted(out, key=lambda d: (d.name.lower(), d.version))


def _outbound_declared() -> str | None:
    """The SPDX expression pyproject.toml declares for Korely Agent itself."""
    import tomllib

    lic = tomllib.loads(PYPROJECT.read_text())["project"].get("license")
    return lic if isinstance(lic, str) else None


def verdicts(deps: list[Dep]) -> list[str]:
    """Every reason this set of dependencies is not acceptable. Empty is pass."""
    bad = []
    for d in deps:
        if d.raw is None:
            bad.append(f"{d.name} {d.version} ({d.where}): no licence declared anywhere")
        elif d.spdx is None:
            bad.append(f"{d.name} {d.version} ({d.where}): licence {d.raw!r} is not one this check knows")
        elif d.spdx in PERMISSIVE:
            continue
        elif d.spdx in COPYLEFT_REVIEWED:
            continue
        else:
            bad.append(f"{d.name} {d.version} ({d.where}): {d.spdx} has never been reviewed")
    return bad


def _table(deps: list[Dep]) -> str:
    rows = ["| Package | Version | Licence | |", "|---|---|---|---|"]
    for d in deps:
        rows.append(f"| `{d.name}` | {d.version} | {d.spdx or '**unresolved**'} | "
                    f"{'direct' if d.direct else 'transitive'} |")
    return "\n".join(rows)


def _counts(deps: list[Dep]) -> str:
    from collections import Counter
    c = Counter(d.spdx or "unresolved" for d in deps)
    return ", ".join(f"{k} ({v})" for k, v in sorted(c.items(), key=lambda kv: -kv[1]))


PREAMBLE = """# Dependencies and their licences

This is the file to hand a security review. It is generated from the packages
actually installed in the image and from the dashboard lockfile, by
`scripts/check_dependency_licences.py`, and the build fails when a licence in
the tree is one nobody has read. A list typed by hand goes stale the first time
somebody adds a package, and a stale list is worse than no list because it gets
believed.

Korely Agent itself is under the Apache License 2.0 ([LICENSE](../LICENSE)),
and that is the licence the check reads every other one against: each licence
below lets its package ship inside an Apache 2.0 distribution, and a strong
copyleft one (GPL, AGPL) would fail the build. Nothing here is licensed to you
by us: each package below is licensed to you by the people who wrote it, under
the licence named next to it.

## What is in the tree

{summary}

## The two that carry a condition

**`psycopg2-binary` is LGPL-3.0-or-later.** {lgpl}

**`certifi` is MPL-2.0.** {mpl}

Neither licence reaches your code or ours, and neither changes the Apache 2.0
licence of Korely Agent. Both are library licences, both are satisfied by using
the library as a library, and we do not modify either package. Both conditions
concern passing the package on; running Korely Agent as a service for other
people, which Apache 2.0 allows, adds none, since neither is the AGPL.

## What `psycopg2-binary` drags in

The `-binary` wheel is not just Python. It carries its own compiled copies of
sixteen shared libraries inside `site-packages`, including libpq, OpenSSL,
Kerberos, OpenLDAP, Cyrus SASL and PCRE2. Two things follow, and both belong in
a review rather than in a footnote:

1. Those libraries have their own licences (OpenSSL 3 under Apache 2.0, MIT
   Kerberos under MIT, OpenLDAP under the OpenLDAP Public License, Cyrus SASL
   under a BSD-style licence, PCRE2 under BSD-3-Clause), and the wheel does not
   ship their licence texts. That is a gap in the wheel, not in the packages.
2. One of the bundled copies is OpenSSL 1.1.1k, a version that left support in
   September 2023. It is reached only through the bundled libpq, and only for
   TLS to the database, which in the normal install is a socket on the same
   machine. It is still an old copy of OpenSSL sitting in the image.

The fix for both is the same and it is small: build `psycopg2` from source
against the `libpq5` already installed in the image instead of taking the
`-binary` wheel. The licence position does not change, since `psycopg2` is LGPL
either way. The sixteen bundled libraries go away. Upstream says the same thing
about the binary wheel and production. This is tracked, not done.

## What the local embedder brings

`onnxruntime`, `numpy` and `sentencepiece` are in every image, for the vectors
computed on the machine (`EMBEDDING_PROVIDER=local`). Two of them are also
compiled code with parts of their own:

1. `numpy`'s Linux wheel carries OpenBLAS and LAPACK (BSD-3-Clause) and the GCC
   runtime: `libgfortran`, under GPL-3.0-or-later WITH the GCC Runtime Library
   Exception, and on x86-64 `libquadmath`, under LGPL-2.1-or-later. The
   exception exists so that a program linked with the runtime is not bound by
   the GPL; `libquadmath` is the LGPL case above, used unmodified as a shared
   library. The wheel ships every licence text in its `dist-info/licenses`.
2. `onnxruntime` (MIT) is built with a long list of other projects, each named
   with its licence in the `ThirdPartyNotices.txt` it ships.

The model those libraries run is not a package and is not in this table. The
image carries it only when built with `EMBEDDING_PROVIDER=local`: Google's
EmbeddingGemma-300m, under the Gemma Terms of Use
(https://ai.google.dev/gemma/terms), downloaded from Hugging Face by the build
on the machine that builds. The terms that apply to it are in
[licenses/GEMMA-TERMS.md](../licenses/GEMMA-TERMS.md), with a copy of the Gemma
Terms of Use beside it, and that build puts both next to the weights with the
notice the Gemma Terms ask for (NOTICE).

## The API page, in the browser

`/docs` is Swagger UI, served by the application from three files in the
package (`korely_agent/docs_ui`): `swagger-ui-dist` 5.33.1 by SmartBear
Software, under the Apache License 2.0, taken from the npm registry and checked
against its published integrity. Its licence and notice are next to the files,
unmodified. It is neither a Python package nor a dashboard dependency, so it is
in neither table below.

## Python, in the image

{python_table}

## Dashboard, in the browser

The dashboard is built to static files, so these are the licences of code that
ends up in what your browser runs. Build tooling (Vite, TypeScript, Tailwind)
does not ship and is not listed.

{dashboard_table}
"""


def render(py: list[Dep], js: list[Dep]) -> str:
    summary = (
        f"- {len(py)} Python packages in the running image: {_counts(py)}.\n"
        f"- {len(js)} JavaScript packages in the dashboard bundle: {_counts(js)}.\n"
        f"- Everything is permissive except two, and both are named below."
    )
    return PREAMBLE.format(
        summary=summary,
        lgpl=COPYLEFT_REVIEWED["LGPL-3.0-or-later"],
        mpl=COPYLEFT_REVIEWED["MPL-2.0"],
        python_table=_table(py),
        dashboard_table=_table(js),
    )


def self_test() -> int:
    """Prove the check can go red. Two holes, both must be caught."""
    cases = [
        (Dep("ghost", "1.0", None, None, False, "python", ""), "no licence declared"),
        (Dep("viral", "1.0", "GPL-3.0-only", None, False, "python", "classifier"), "is not one this check knows"),
    ]
    # A licence that normalises but was never reviewed must also fail.
    SPDX["GPL-3.0-only"] = "GPL-3.0-only"
    cases.append((Dep("viral2", "1.0", "GPL-3.0-only", "GPL-3.0-only", False, "python", "classifier"),
                  "never been reviewed"))
    failures = []
    for dep, expected in cases:
        found = verdicts([dep])
        if not found:
            failures.append(f"{dep.name}: passed when it should have failed")
        elif expected not in found[0]:
            failures.append(f"{dep.name}: failed for the wrong reason: {found[0]}")
    del SPDX["GPL-3.0-only"]
    # And a clean one must still pass.
    if verdicts([Dep("fine", "1.0", "MIT", "MIT", False, "python", "classifier")]):
        failures.append("a plain MIT package was rejected")
    for f in failures:
        print("SELF-TEST FAIL:", f)
    print(f"self-test: {len(cases) + 1 - len(failures)}/{len(cases) + 1}")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="regenerate docs/DEPENDENCIES.md")
    ap.add_argument("--self-test", action="store_true", help="prove the check can go red")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    declared = _outbound_declared()
    if declared != OUTBOUND:
        print(f"pyproject.toml declares {declared!r}, and every licence here was "
              f"reviewed against {OUTBOUND!r}. Review them again, then change OUTBOUND.")
        return 1

    from importlib.metadata import distributions
    py = _python_deps(distributions())
    js = _dashboard_deps()

    bad = verdicts(py + js)
    if bad:
        print("Licences that nobody has read:")
        for b in bad:
            print("  -", b)
        print("\nRead it, then add it to SPDX and to PERMISSIVE or COPYLEFT_REVIEWED")
        print("in scripts/check_dependency_licences.py, with the condition written out.")
        return 1

    text = render(py, js)
    if args.write:
        OUT.write_text(text)
        print(f"wrote {OUT.relative_to(ROOT)}: {len(py)} Python, {len(js)} dashboard")
        return 0

    if not OUT.exists():
        print(f"{OUT.relative_to(ROOT)} does not exist. Run with --write.")
        return 1
    if OUT.read_text() != text:
        print(f"{OUT.relative_to(ROOT)} is stale. Run with --write and commit the result.")
        return 1
    print(f"licences: {len(py)} Python, {len(js)} dashboard, all accounted for")
    return 0


if __name__ == "__main__":
    sys.exit(main())
