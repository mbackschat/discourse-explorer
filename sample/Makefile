# Sample-local Makefile.
#
# When invoked as `make -C sample <target>` (the documented form), make's
# working directory is sample/, so `docker-compose.yml` and `.env` are
# referenced by their plain names. The `test` target dips back up to the
# project root with `cd ..` because uv test discovery wants to run from
# there.
#
# Targets:
#   test     run sample-local unittest discovery
#   up       bring up the Discourse + postgres + redis stack (detached)
#   down     stop the stack (preserves named volumes)
#   nuke     stop the stack AND delete named volumes (full reset)
#   logs     follow logs from all stack services
#   status   show stack container status
#   api-key  extract an admin API key from the running discourse container
#            and write DISCOURSE_API_KEY back to sample/.env. Idempotent —
#            no-op if a key is already set. Run AFTER `make up` has finished
#            bootstrapping (the admin user must exist).

COMPOSE := docker compose -f docker-compose.yml --env-file .env

.PHONY: test up down nuke logs status api-key check-env

# Run sample-local tests. The project-wide test runner does not pick these up
# — discovery is rooted at sample/tests/.
test:
	cd .. && uv run python -m unittest discover sample/tests

# Guard: fail loudly if sample/.env is missing rather than letting docker
# compose emit a confusing "variable not set" warning chain.
check-env:
	@test -f .env || { \
	  echo "ERROR: sample/.env is missing."; \
	  echo "Copy the template first:  cp sample/.env.example sample/.env"; \
	  echo "Then edit DISCOURSE_PASSWORD + DISCOURSE_DATABASE_PASSWORD."; \
	  exit 1; \
	}

up: check-env
	$(COMPOSE) up -d

down: check-env
	$(COMPOSE) down

nuke: check-env
	$(COMPOSE) down -v

logs: check-env
	$(COMPOSE) logs -f

status: check-env
	$(COMPOSE) ps

# Extract an admin API key from the running discourse container.
#
# Uses Discourse's built-in rake task `api_key:create_master[<description>]`
# inside the bitnami image. Three bitnami-specific quirks the recipe handles:
#   1. RAILS_ENV defaults to "development" in the container, but the dev
#      Gemfile group (with `listen`) isn't bundled — must export production.
#   2. `ruby` / `bundle` / `rake` are at /opt/bitnami/ruby/bin which is NOT
#      in the default exec PATH — we prepend it explicitly.
#   3. The rake task prints initialization warnings to stdout before the
#      hex key on the final line; we filter to the 64-char hex pattern.
#
# Idempotent: if DISCOURSE_API_KEY is already non-empty in .env, the target
# is a no-op. The `sed -i.bak ... && rm .env.bak` form works on both BSD
# (macOS default) and GNU sed — `-i` alone has incompatible semantics
# between the two.
api-key: check-env
	@if grep -q '^DISCOURSE_API_KEY=..*' .env; then \
	  echo "DISCOURSE_API_KEY already set in sample/.env — skipping."; \
	  exit 0; \
	fi
	@echo "Extracting admin API key from running container..."
	@key=$$($(COMPOSE) exec -T -e RAILS_ENV=production discourse bash -c 'cd /opt/bitnami/discourse && PATH=/opt/bitnami/ruby/bin:$$PATH bin/rake "api_key:create_master[sample seeder]"' 2>/dev/null | tr -d '\r' | grep -E '^[a-f0-9]{64}$$' | tail -1); \
	if [ -z "$$key" ]; then \
	  echo "ERROR: failed to extract API key. Is the stack up + bootstrapped?"; \
	  echo "  Try: make logs   (look for 'Started GET /' before retrying)"; \
	  exit 1; \
	fi; \
	if grep -q '^DISCOURSE_API_KEY=' .env; then \
	  sed -i.bak "s|^DISCOURSE_API_KEY=.*|DISCOURSE_API_KEY=$$key|" .env && rm -f .env.bak; \
	else \
	  echo "DISCOURSE_API_KEY=$$key" >> .env; \
	fi; \
	echo "Wrote DISCOURSE_API_KEY to sample/.env"
