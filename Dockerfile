# The screen, built first. Node is here and not in the final image: the result
# is a directory of static files, and the toolchain that produced them has no
# business on a machine serving other people's data.
#
# On the builder's own platform ($BUILDPLATFORM), once, for every image: static
# files are the same for amd64 and arm64. Built for each target, the arm64 one
# ran `npm ci` under QEMU, and on 7 Oct 2026 (0.1.20, the first build with
# Tailwind 4's native packages) it hung for an hour in the image workflow and
# again on the rerun, while the same install on amd64 took seconds.
FROM --platform=$BUILDPLATFORM node:20-alpine AS dashboard
WORKDIR /dashboard
# The manifest and the lock first, so the install is cached until they change.
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
# Then the sources, by name, and never the whole directory. `COPY dashboard/ ./`
# used to sit here and brought the builder's own dashboard/node_modules with it,
# on top of what `npm ci` had just installed for the image's platform: built on
# a Mac, the Linux image got the Mac's packages. .dockerignore keeps them out of
# the context too, but the image no longer depends on it being there.
# tests/test_build_context.py fails if a file of the dashboard is left out here.
# Tailwind v4 has no tailwind.config.js: the theme is in src/index.css.
COPY dashboard/index.html dashboard/vite.config.ts dashboard/tsconfig.json \
     dashboard/tsconfig.node.json dashboard/postcss.config.js ./
COPY dashboard/public ./public
COPY dashboard/src ./src
# `npm run build` is `tsc --noEmit && vite build`: a type error fails the image
# rather than shipping a page that breaks on a route nobody clicked in dev.
RUN npm run build


# The local embedder's model (core/embedding_model.py, core/local_embedder.py),
# the default since 0.1.19: EMBEDDING_PROVIDER in .env, which
# docker-compose.yml passes here, local unless it says gemini. With gemini this
# stage leaves an empty folder and the image is 1.2 GB lighter.
#
# A stage of its own, fed one file: the 1.2 GB it downloads is cached on the
# pinned revision and the hashes written in that file, so a new version of the
# code does not download the weights again. Every file is checked against its
# sha256, and a build with other weights stops here.
#
# EMBEDDING_MODEL_MIRROR: a host that serves the Hugging Face layout, for a
# builder that cannot reach huggingface.co.
# On the builder's platform too: the weights are the same files for every
# target, downloaded and checked once instead of once per platform under QEMU.
FROM --platform=$BUILDPLATFORM python:3.11-slim AS model
ARG EMBEDDING_PROVIDER=local
ARG EMBEDDING_MODEL_MIRROR=https://huggingface.co
COPY korely_agent/core/embedding_model.py /fetch/embedding_model.py
# Read as the application reads it (config.py): "Local " is local.
RUN mkdir -p /models \
 && if [ "$(printf '%s' "$EMBEDDING_PROVIDER" | tr -d ' ' | tr 'A-Z' 'a-z')" = "local" ]; then \
      python /fetch/embedding_model.py /models/embeddinggemma-300m \
        --mirror "$EMBEDDING_MODEL_MIRROR"; \
    fi
# The terms next to the weights, as the Gemma Terms of Use ask of every copy:
# the additional terms approved on 2026-10-01 and the copy of the
# Gemma Terms (licenses/), beside the NOTICE the download wrote. After the
# download, so that a change to these texts does not download the weights again.
COPY licenses/GEMMA-TERMS.md licenses/gemma-terms-of-use.txt /fetch/terms/
RUN if [ -d /models/embeddinggemma-300m ]; then \
      cp /fetch/terms/GEMMA-TERMS.md /fetch/terms/gemma-terms-of-use.txt \
         /models/embeddinggemma-300m/; \
    fi


# Runtime image.
#
# The image this replaces is 2.3 GB, because the process it ran was also a
# note-taking app, a meeting recorder and a chat product. This one carries a
# memory service.
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# libpq for psycopg2 at runtime; the build toolchain is not kept.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The generated lock, not requirements.txt: every package, transitive ones
# included, at one exact version, each file checked against its sha256. Two
# builds of the same tag install the same code, whenever and wherever they run
# (how the lock is made: the top of requirements.txt).
COPY requirements/requirements.lock.txt ./
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libpq-dev \
 && pip install --no-cache-dir --require-hashes -r requirements.lock.txt \
 && apt-get purge -y gcc libpq-dev \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# The local embedder's weights, or an empty folder (stage `model`). Before the
# code, so that a change to the code does not rewrite 1.2 GB of layer; and
# outside /app, next to the code and like it root's: the service only reads
# them.
COPY --from=model /models /opt/korely-models
# The embedder this image was built for is the service's default: the image
# without the model computes its vectors with Gemini, the one with the model
# here, unless .env says otherwise. Without this line the light image started
# as local and had no model to compute with.
ARG EMBEDDING_PROVIDER=local
ENV EMBEDDING_PROVIDER=${EMBEDDING_PROVIDER}

COPY alembic/alembic.ini ./
COPY alembic ./alembic
# Not scripts/: every file there is a developer's tool, run from a checkout,
# and check_migration.py starts by dropping the schema DATABASE_URL points at,
# which in here is the customer's. What an operator runs in this container is
# `python -m korely_agent.manage`, part of the package below.
# tests/test_build_context.py keeps scripts/ out of the image and the context.
# The version lives in a file next to the source, and the source is what gets
# copied. Without this line the release image answers `"version": "unknown"`
# while the development image, which mounts the repository, answers correctly:
# the one place the number is actually asked for is the one place it was
# missing. scripts/check_the_image_knows_its_version.sh is the check.
COPY VERSION ./
# The licence that governs this copy, and its notices, travel with it: the
# Apache License 2.0 asks for both in every copy distributed (section 4), and
# the image is one (the open-source check of 17 Oct 2026 found neither in
# 0.1.39).
COPY LICENSE NOTICE ./
COPY korely_agent ./korely_agent
# The built screen. Served by the application rather than by the proxy, so the
# install stays two containers plus a proxy and there is one artefact to ship.
COPY --from=dashboard /dashboard/dist ./static
COPY deploy/entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Not root. The service reads and writes one database and nothing on disk,
# so nothing under /app is its to write: every file above was copied as root
# and stays root's, readable by the service and writable by nobody but root
# (until 3 Nov 2026 `chown -R korely:korely /app` handed the code to the
# service's user, and a process of the service taken over by a crafted input
# could have rewritten the application it ran in: the security review of
# 3 Nov 2026).
# PYTHONDONTWRITEBYTECODE above is what lets Python run from a tree it cannot
# write; what an operator's command writes (`manage review-objects --out`)
# goes to a folder of its own or to /tmp, which manage.py says.
# uid 1000 named, not assumed: docker-compose.yml hands ./facts to this uid.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin korely
USER korely

EXPOSE 8000
# Healthy once /health says `ready`: the port answers while the local
# embedder loads its model at the start (7.5 s with a cold file cache, 1.3 s
# warm, measured with two CPUs on 7 Nov 2026), and until 1.0.26 this check
# passed in that window, so whatever waited for `healthy` sent traffic to a
# process not ready for it. Not ready is not a failure: a failing part makes
# `status` `degraded` and stays healthy here, as before. The start period
# covers the load: failures in it do not count (Docker 25 and later check
# every 5 s during it, older ones after the interval).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import json,sys,urllib.request;sys.exit(0 if json.load(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4)).get('ready') else 1)" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
# FORWARDED_ALLOW_IPS: the peers believed when they say, with X-Forwarded-For
# and X-Forwarded-Proto, the client's address and scheme. Without the scheme
# the app believes it is serving plain HTTP behind Caddy's TLS and builds
# absolute URLs to match (a redirect of /mcp once said http:// and an MCP
# client resent `Authorization: Bearer kor_self_...` in clear text); without
# the address every client is Caddy's, for the login lock, the address in the
# LOGIN audit row and the caps per network.
#
# `caddy`: the service of the shipped compose that stands in front, by NAME,
# resolved again when its address changes (korely_agent/core/proxies.py,
# which reads the headers with uvicorn's own middleware). Up to 1.0.22 this
# was `*`, every peer believed, and whoever reached port 8000 directly, a
# container on the compose network or the port published by mistake, chose
# its own address (the review of 6 Nov 2026; the founder's decision of 14 Nov
# 2026: trust the proxy alone). A proxy of yours in Caddy's place is named
# here instead, by name, address or network (docs/guide/operations.md,
# "Your own proxy in front"). The shipped Caddy writes X-Forwarded-For itself, with
# the connection's own address, and drops what the client sent
# (deploy/Caddyfile).
#
# An environment variable, not a flag in CMD, so that one line of .env
# changes it (audit of 17 Oct 2026). uvicorn's own reading is off
# (--no-proxy-headers): the application's is the one.
ENV FORWARDED_ALLOW_IPS=caddy
# --ws none: the app has no WebSocket route, and uvicorn answering a handshake
# itself wrote its path and query on uvicorn.error, where no filter of the app
# reaches (`"WebSocket /v1/users/<id>/memories?query=..." 403`). Without it an
# Upgrade request is an HTTP request like any other, with the access line of
# main.loggable_path (tests/test_no_websocket_line.py).
#
# --timeout-graceful-shutdown 5: at a stop (SIGTERM) uvicorn waits for the open
# connections to end before it runs the app's shutdown, and with no limit it
# waits for ever on one that never ends, an MCP client's open stream: Docker's
# stop timeout (10 s by default) ran out first, the process was killed, and
# the shutdown, which gives the worker's claimed memories back to the queue
# (worker.give_back), never ran (the review of 13 Nov 2026, uvicorn 0.52.4).
# After 5 s the requests still running are cancelled and the shutdown runs,
# with 5 s left before Docker's kill.
CMD ["uvicorn", "korely_agent.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-proxy-headers", "--ws", "none", "--timeout-graceful-shutdown", "5"]
