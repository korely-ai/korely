# Everything runs in Docker. Hosts differ (macOS ships Python 3.9; this needs
# 3.11) and a test suite that only passes on the author's laptop is not a test
# suite.
#
# CURDIR and not PWD: `make -C korely-agent test-all` from another directory
# leaves PWD at the caller, so Docker mounted the wrong repository and the suite
# failed to collect. It looked like three real errors for a minute. CURDIR is
# the directory make is actually working in.
#
# The test image is named after what it installs. It used to be one fixed name,
# built once and reused for ever: the lock could move and the suite kept
# running on whatever that machine had installed weeks earlier, which is how
# the tests of 0.1.8 ran on older packages than the 0.1.8 image shipped. Now a
# changed lock (or Dockerfile.dev) is a different name, so `make image` builds
# it; `korely-agent-dev:latest` follows for scripts that name it directly.
# cksum is POSIX, so this works on any host with make; it is a cache key, not a
# security check (the hashes in the lock are that).
IMG_NAME := korely-agent-dev
IMG := $(IMG_NAME):$(shell cat dev/Dockerfile.dev requirements/requirements-dev.lock.txt 2>/dev/null | cksum | cut -d' ' -f1)
# The suite and the migrations run from a copy of the checkout made inside
# the container, in /w, and the checkout is mounted read-only at /src: on a
# Mac, Docker Desktop's file sharing loses the working directory of a process
# in the checkout for a moment whenever a container starts or stops, and a
# process of the suite that asked for it then died (scripts/from_a_copy.sh
# has the measures, and what the copy leaves out). The copy is 22 MB, about
# 890 files: 1.1 to 7.5 s, median 3.6, in ten copies on a busy Mac (8 Nov
# 2026), against some 400 s of the suite in 4 processes.
CHECKOUT_RO := -v $(CURDIR):/src:ro -w /w -e PYTHONPATH=/w
FROM_A_COPY := sh /src/scripts/from_a_copy.sh
# The image the lock is resolved in: the base both images start from.
LOCK_PY := python:3.11-slim
# The test database. Every checkout on a machine (worktrees, other sessions)
# shares the one Postgres on 55433, and the suite drops and creates every table
# of the database it is given, so two runs on one name wreck each other. A run
# names its own: `make test-all TEST_DB=korely_test_r108`, or TEST_DB in the
# environment, which is how a caller of make (scripts/release.sh) passes it.
# `test-db` creates it when it is missing. The name must say `test`, the rule
# scripts/check_migration.py --wipe holds, in lower case letters, digits and
# `_`, the names Postgres keeps as typed. It reaches test-db's shell from the
# environment, never pasted into the command, so a name with a quote in it is
# refused like any other and not run.
TEST_DB ?= korely_test
export TEST_DB
TEST_DB_URL = postgresql://korely:test@localhost:55433/$(TEST_DB)
TEST_COMPOSE := docker compose -p korely-agent-test -f dev/docker-compose.test.yml
# Same idea for the product image the licence check reads.
LIC_IMG := korely-agent:licences-$(shell cat Dockerfile requirements/requirements.lock.txt 2>/dev/null | cksum | cut -d' ' -f1)

.PHONY: image release test test-all dashboard-test migrate-check lock image-check licences licences-write db-up db-down test-db shell clean
image:
	@docker image inspect $(IMG) >/dev/null 2>&1 || \
		docker build -q -f dev/Dockerfile.dev -t $(IMG) . >/dev/null
	@docker tag $(IMG) $(IMG_NAME):latest

# Regenerate the dependency locks after editing requirements.txt or
# requirements-dev.txt. Existing pins are kept: ARGS="-P fastapi" moves one
# package, ARGS=--upgrade moves all of them. Needs the network (every file of
# a new pin is downloaded to be hashed, streamed, nothing kept on disk) and
# runs in the images' base Python, because a lock resolved on the Mac could pin
# something Linux does not get. scripts/requirements_lock.py has the details.
lock:
	docker run --rm -v $(CURDIR):/w -w /w $(LOCK_PY) python scripts/requirements_lock.py lock $(ARGS)

# Is a built product image exactly the lock? `pip check` inside it, then its
# `pip freeze` compared with requirements.lock.txt by the guard, which runs in
# the test image because the product image carries no tooling of its own.
#     make image-check IMAGE=korely-agent:release-0.1.9
image-check: image
	@test -n "$(IMAGE)" || { echo "usage: make image-check IMAGE=<product image>" >&2; exit 2; }
	@docker run --rm --entrypoint pip $(IMAGE) check
	@docker run --rm --entrypoint pip $(IMAGE) freeze --all | \
		docker run --rm -i -v $(CURDIR):/w -w /w $(IMG) \
		python scripts/requirements_lock.py check --installed product --freeze -

# Multi-arch release image. Building only on the developer's machine ships an
# arm64 image, and most servers people rent are x86: the image simply will not
# start there, with a message about exec format that says nothing useful.
release:
	docker buildx build --platform linux/amd64,linux/arm64 \
		-t korely-agent:$(or $(VERSION),dev) .

# Unit tests: no database, no network.
test: image
	@docker run --rm $(CHECKOUT_RO) $(IMG) $(FROM_A_COPY) \
		sh -c 'python scripts/check_the_version.py && python -m pytest -q -m "not db"'

# Everything, including the tests that need real Postgres with pgvector, and
# the dashboard's own check (dashboard-test), so that the gate and
# scripts/release.sh, which call this, run it too. The Python suite in one
# process, unless WORKERS says how many on make's command line:
#     make test-all TEST_DB=korely_test_r108 WORKERS=4
# runs it in 4 processes (pytest-xdist), each on a database of its own next to
# TEST_DB (korely_test_r108_gw0, _gw1, ..., made and dropped by the suite,
# tests/conftest.py), then the tests marked `serial` in one process on
# TEST_DB. Same tests, same result; scripts/test_all.sh has the details.
# WORKERS is read from the command line only, never from the environment
# (`:=`, which an environment variable does not override and a command-line
# one does): a WORKERS left in a shell does not turn scripts/release.sh or the
# CI, which run `make test-all`, into a parallel run. Mind the machine: every
# worker is a full process of the suite with its own connections to the one
# Postgres.
# A Ctrl-C stops it: with --init, PID 1 is an init of Docker's own (tini),
# which hands a signal to every process of the run (TINI_KILL_PROCESS_GROUP),
# where a shell as PID 1 would ignore it; pytest and its workers then end as
# on a Ctrl-C at a terminal, and each drops its database. `docker stop` sends
# the same SIGINT, not TERM, which ends a Python process on the spot and
# leaves its database behind, and waits up to a minute for it.
# It runs from a copy of the checkout made in the container (FROM_A_COPY,
# above), so nothing it writes reaches the checkout, which is mounted
# read-only. PYTHONDONTWRITEBYTECODE stays, as in the public copy's check
# (scripts/make_public_repo_check.sh): every run compiles what it imports,
# as the runs measured above did. It was put there on 7 Nov 2026 for a
# process that lost its working directory (2 runs out of 3 with the bytecode
# written into the checkout, 0 out of 3 without); a probe of 8 Nov 2026 found
# the cause elsewhere, in Docker Desktop's file sharing while containers
# start or stop, which the copy removes (scripts/from_a_copy.sh).
WORKERS :=
export WORKERS
test-all: image dashboard-test test-db
	@docker run --rm --init --stop-signal SIGINT --stop-timeout 60 -e TINI_KILL_PROCESS_GROUP=1 \
		--network host $(CHECKOUT_RO) -e PYTHONDONTWRITEBYTECODE=1 \
		-e TEST_DATABASE_URL=$(TEST_DB_URL) \
		-e DATABASE_URL=$(TEST_DB_URL) \
		-e WORKERS \
		$(IMG) $(FROM_A_COPY) sh scripts/test_all.sh

# What each read of the dashboard sends, run with node: `npm ci && npm test`
# (dashboard/scripts/check_the_reads.mjs), in the image the CI and the public
# copy's check build the dashboard with. The Python suite reads the
# dashboard's source as text and cannot run it; until 6 Nov 2026 nothing at
# the gate or before a tag ran this. The sources are copied into the
# container without the host's node_modules and dist, and nothing is written
# to the checkout. Needs the npm registry.
dashboard-test:
	@docker run --rm -v $(CURDIR)/dashboard:/src:ro node:20-alpine sh -c \
		'mkdir /tmp/d && tar -C /src --exclude=./node_modules --exclude=./dist -cf - . | tar -C /tmp/d -xf - \
		 && cd /tmp/d && npm ci --no-audit --no-fund --loglevel=error && npm test'

# Wipe a database, apply every migration, compare the result to the models.
# The check the extracted codebase did not have when a bad migration took
# production down in a boot loop.
migrate-check: image test-db
	@docker run --rm --network host $(CHECKOUT_RO) \
		-e DATABASE_URL=$(TEST_DB_URL) \
		$(IMG) $(FROM_A_COPY) python scripts/check_migration.py --wipe $(TEST_DB)

# Licences of everything in the tree, read off the packages that are actually
# installed. `licences` fails when the file is stale or when a licence in the
# tree is one nobody has read; `licences-write` regenerates it. The image used
# is the release image, not the dev one, because the question is what ships.
licences:
	@docker image inspect $(LIC_IMG) >/dev/null 2>&1 || \
		docker build -q -t $(LIC_IMG) . >/dev/null
	@docker run --rm --entrypoint python -v $(CURDIR):/w -w /w $(LIC_IMG) \
		scripts/check_dependency_licences.py --self-test
	@docker run --rm --entrypoint python -v $(CURDIR):/w -w /w $(LIC_IMG) \
		scripts/check_dependency_licences.py

licences-write:
	@docker build -q -t $(LIC_IMG) . >/dev/null
	@docker run --rm --entrypoint python -v $(CURDIR):/w -w /w $(LIC_IMG) \
		scripts/check_dependency_licences.py --write

# The test server allows more connections than Postgres's default, for runs in
# several processes beside the others: dev/test_db_settings.sh says why, and
# why not in the compose file.
db-up:
	@$(TEST_COMPOSE) up -d --wait >/dev/null
	@sh dev/test_db_settings.sh $(TEST_COMPOSE)

db-down:
	@$(TEST_COMPOSE) down -v >/dev/null

test-db: db-up
	@printf '%s\n' "$$TEST_DB" | grep -Eq '^[a-z0-9_]*test[a-z0-9_]*$$' || { \
		echo "refused: TEST_DB=\"$$TEST_DB\" must say test, in lower case letters, digits and _" >&2; \
		exit 2; }
	@! printf '%s\n' "$$TEST_DB" | grep -Eq '_gw[0-9]+$$' || { \
		echo "refused: TEST_DB=\"$$TEST_DB\" ends like a worker's database of a parallel run (<run>_gw<n>)," \
			"which that run drops at its end; name it otherwise" >&2; \
		exit 2; }
	@$(TEST_COMPOSE) exec -T db psql -U korely -d postgres -tAc \
		"SELECT 1 FROM pg_database WHERE datname = '$$TEST_DB'" | grep -q 1 || \
		$(TEST_COMPOSE) exec -T db createdb -U korely "$$TEST_DB"

shell: image
	@docker run --rm -it -v $(CURDIR):/w -w /w -e PYTHONPATH=/w $(IMG) bash

clean:
	@docker image ls --format '{{.Repository}}:{{.Tag}}' | \
		grep -E '^($(IMG_NAME):|korely-agent:licences)' | xargs docker image rm -f >/dev/null 2>&1 || true
	@rm -rf .venv .pytest_cache
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
