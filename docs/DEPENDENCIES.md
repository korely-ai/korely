# Dependencies and their licences

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

- 62 Python packages in the running image: MIT (30), BSD-3-Clause (13), Apache-2.0 (8), MPL-2.0 (1), MIT-0 (1), Apache-2.0 OR BSD-3-Clause (1), MIT AND PSF-2.0 (1), BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 (1), Apache-2.0 OR BSD-2-Clause (1), LGPL-3.0-or-later (1), BSD-2-Clause (1), MIT OR Apache-2.0 (1), PSF-2.0 (1), Apache-2.0 OR MIT (1).
- 38 JavaScript packages in the dashboard bundle: MIT (37), ISC (1).
- Everything is permissive except two, and both are named below.

## The two that carry a condition

**`psycopg2-binary` is LGPL-3.0-or-later.** Used as a library, unmodified, dynamically linked. The obligation is to say so, to ship the licence text (the wheel carries it), and to leave the user able to replace it. We do all three and we do not patch the package.

**`certifi` is MPL-2.0.** File-level copyleft. It bites only on modified files of that project, and we modify none. Shipping it unchanged inside the image is the case the licence was written for.

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

| Package | Version | Licence | |
|---|---|---|---|
| `alembic` | 1.19.2 | MIT | direct |
| `annotated-doc` | 0.0.5 | MIT | transitive |
| `annotated-types` | 0.8.0 | MIT | transitive |
| `anyio` | 4.15.1 | MIT | direct |
| `attrs` | 26.1.0 | MIT | transitive |
| `certifi` | 2026.7.22 | MPL-2.0 | transitive |
| `cffi` | 2.1.1 | MIT-0 | transitive |
| `charset-normalizer` | 3.5.1 | MIT | transitive |
| `click` | 8.5.0 | BSD-3-Clause | transitive |
| `cryptography` | 50.0.1 | Apache-2.0 OR BSD-3-Clause | direct |
| `distro` | 1.9.0 | Apache-2.0 | transitive |
| `fastapi` | 0.141.1 | MIT | direct |
| `flatbuffers` | 25.12.19 | Apache-2.0 | transitive |
| `google-auth` | 2.58.1 | Apache-2.0 | transitive |
| `google-genai` | 1.75.0 | Apache-2.0 | direct |
| `greenlet` | 3.5.6 | MIT AND PSF-2.0 | transitive |
| `h11` | 0.16.0 | MIT | transitive |
| `httpcore` | 1.0.9 | BSD-3-Clause | direct |
| `httptools` | 0.8.0 | MIT | transitive |
| `httpx` | 0.28.1 | BSD-3-Clause | direct |
| `httpx-sse` | 0.4.3 | MIT | transitive |
| `idna` | 3.20 | BSD-3-Clause | transitive |
| `jsonschema` | 4.26.0 | MIT | transitive |
| `jsonschema-specifications` | 2025.9.1 | MIT | transitive |
| `Mako` | 1.4.3 | MIT | transitive |
| `MarkupSafe` | 3.0.3 | BSD-3-Clause | transitive |
| `mcp` | 1.30.0 | MIT | direct |
| `numpy` | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | direct |
| `onnxruntime` | 1.30.0 | MIT | direct |
| `packaging` | 26.3 | Apache-2.0 OR BSD-2-Clause | transitive |
| `pgvector` | 0.5.0 | MIT | direct |
| `pip` | 24.0 | MIT | transitive |
| `protobuf` | 7.36.2 | BSD-3-Clause | transitive |
| `psycopg2-binary` | 2.9.13 | LGPL-3.0-or-later | direct |
| `pyasn1` | 0.6.4 | BSD-2-Clause | transitive |
| `pyasn1_modules` | 0.4.2 | BSD-3-Clause | transitive |
| `pycparser` | 3.0 | BSD-3-Clause | transitive |
| `pydantic` | 2.13.5 | MIT | direct |
| `pydantic-settings` | 2.15.0 | MIT | direct |
| `pydantic_core` | 2.46.5 | MIT | direct |
| `PyJWT` | 2.15.0 | MIT | transitive |
| `python-dotenv` | 1.2.3 | BSD-3-Clause | transitive |
| `python-multipart` | 0.0.32 | Apache-2.0 | transitive |
| `PyYAML` | 6.0.3 | MIT | transitive |
| `referencing` | 0.37.0 | MIT | transitive |
| `requests` | 2.34.2 | Apache-2.0 | transitive |
| `rpds-py` | 2026.6.3 | MIT | transitive |
| `sentencepiece` | 0.2.2 | Apache-2.0 | direct |
| `setuptools` | 79.0.1 | MIT | transitive |
| `sniffio` | 1.3.1 | MIT OR Apache-2.0 | transitive |
| `SQLAlchemy` | 2.0.54 | MIT | direct |
| `sse-starlette` | 3.4.11 | BSD-3-Clause | transitive |
| `starlette` | 1.7.0 | BSD-3-Clause | direct |
| `tenacity` | 9.1.4 | Apache-2.0 | transitive |
| `typing-inspection` | 0.4.4 | MIT | transitive |
| `typing_extensions` | 4.16.0 | PSF-2.0 | transitive |
| `urllib3` | 2.8.0 | MIT | transitive |
| `uvicorn` | 0.52.4 | BSD-3-Clause | direct |
| `uvloop` | 0.22.1 | Apache-2.0 OR MIT | transitive |
| `watchfiles` | 1.3.0 | MIT | transitive |
| `websockets` | 16.1.1 | BSD-3-Clause | transitive |
| `wheel` | 0.46.3 | MIT | transitive |

## Dashboard, in the browser

The dashboard is built to static files, so these are the licences of code that
ends up in what your browser runs. Build tooling (Vite, TypeScript, Tailwind)
does not ship and is not listed.

| Package | Version | Licence | |
|---|---|---|---|
| `agent-base` | 6.0.2 | MIT | transitive |
| `asynckit` | 0.4.0 | MIT | transitive |
| `axios` | 1.20.0 | MIT | direct |
| `call-bind-apply-helpers` | 1.0.2 | MIT | transitive |
| `clsx` | 2.1.1 | MIT | direct |
| `combined-stream` | 1.0.8 | MIT | transitive |
| `cookie` | 1.1.1 | MIT | transitive |
| `debug` | 4.4.3 | MIT | transitive |
| `delayed-stream` | 1.0.0 | MIT | transitive |
| `dunder-proto` | 1.0.1 | MIT | transitive |
| `es-define-property` | 1.0.1 | MIT | transitive |
| `es-errors` | 1.3.0 | MIT | transitive |
| `es-object-atoms` | 1.1.2 | MIT | transitive |
| `es-set-tostringtag` | 2.1.0 | MIT | transitive |
| `follow-redirects` | 1.16.0 | MIT | transitive |
| `form-data` | 4.0.6 | MIT | transitive |
| `function-bind` | 1.1.2 | MIT | transitive |
| `get-intrinsic` | 1.3.0 | MIT | transitive |
| `get-proto` | 1.0.1 | MIT | transitive |
| `gopd` | 1.2.0 | MIT | transitive |
| `has-symbols` | 1.1.0 | MIT | transitive |
| `has-tostringtag` | 1.0.2 | MIT | transitive |
| `hasown` | 2.0.4 | MIT | transitive |
| `https-proxy-agent` | 5.0.1 | MIT | transitive |
| `js-tokens` | 4.0.0 | MIT | transitive |
| `loose-envify` | 1.4.0 | MIT | transitive |
| `lucide-react` | 0.469.0 | ISC | direct |
| `math-intrinsics` | 1.1.0 | MIT | transitive |
| `mime-db` | 1.52.0 | MIT | transitive |
| `mime-types` | 2.1.35 | MIT | transitive |
| `ms` | 2.1.3 | MIT | transitive |
| `proxy-from-env` | 2.1.0 | MIT | transitive |
| `react` | 18.3.1 | MIT | direct |
| `react-dom` | 18.3.1 | MIT | direct |
| `react-router` | 7.18.4 | MIT | direct |
| `scheduler` | 0.23.2 | MIT | transitive |
| `set-cookie-parser` | 2.7.2 | MIT | transitive |
| `tailwind-merge` | 3.7.0 | MIT | direct |
