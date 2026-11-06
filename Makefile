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
RUN := docker run --rm -v $(CURDIR):/w -w /w -e PYTHONPATH=/w $(IMG)
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

.PHONY: image release test test-all migrate-check lock image-check licences licences-write db-up db-down test-db shell clean
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
	@$(RUN) python scripts/check_the_version.py
	@$(RUN) python -m pytest -q -m "not db"

# Everything, including the tests that need real Postgres with pgvector.
test-all: image test-db
	@docker run --rm --network host -v $(CURDIR):/w -w /w -e PYTHONPATH=/w \
		-e TEST_DATABASE_URL=$(TEST_DB_URL) \
		-e DATABASE_URL=$(TEST_DB_URL) \
		$(IMG) python -m pytest -q

# Wipe a database, apply every migration, compare the result to the models.
# The check the extracted codebase did not have when a bad migration took
# production down in a boot loop.
migrate-check: image test-db
	@docker run --rm --network host -v $(CURDIR):/w -w /w -e PYTHONPATH=/w \
		-e DATABASE_URL=$(TEST_DB_URL) \
		$(IMG) python scripts/check_migration.py --wipe $(TEST_DB)

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

db-up:
	@$(TEST_COMPOSE) up -d --wait >/dev/null

db-down:
	@$(TEST_COMPOSE) down -v >/dev/null

test-db: db-up
	@printf '%s\n' "$$TEST_DB" | grep -Eq '^[a-z0-9_]*test[a-z0-9_]*$$' || { \
		echo "refused: TEST_DB=\"$$TEST_DB\" must say test, in lower case letters, digits and _" >&2; \
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
