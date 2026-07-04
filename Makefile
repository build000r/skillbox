COMPOSE := docker compose

# Resolve operator env file for non-secret overrides. Secrets moved out of the
# workspace bind mount to $(SKILLBOX_STATE_ROOT)/operator/.env, so compose can no
# longer auto-load .env from the project dir. Prefer the relocated file, fall back
# to a legacy repo-root .env, else omit --env-file (compose ${VAR:-default}s hold).
_STATE_ROOT := $(if $(strip $(SKILLBOX_STATE_ROOT)),$(SKILLBOX_STATE_ROOT),./.skillbox-state)
_OPERATOR_ENV := $(firstword $(wildcard $(_STATE_ROOT)/operator/.env) $(wildcard ./.env))
_ENV_FILE_ARG := $(if $(_OPERATOR_ENV),--env-file $(_OPERATOR_ENV),)

# Resolve monoserver layer: per-client override when focused, fat default otherwise.
# Read the focused client id. A momentarily-invalid .focus.json silently falls
# back to empty here, which flips _MONOSERVER_LAYER to the fat default — i.e. it
# changes WHICH FILESYSTEM gets mounted. Keep the empty fallback (so a clean
# absent-file case stays quiet) but warn on stderr when the file EXISTS yet
# fails to parse, so that silent mis-steer becomes visible.
_FOCUS_CLIENT := $(shell python3 -c "import json,os;p='workspace/.focus.json';\
print(json.load(open(p)).get('client_id','')) if os.path.exists(p) else print('')" 2>/dev/null \
|| (echo >&2 'make: warning: workspace/.focus.json exists but failed to parse; falling back to monoserver default mount'; echo ''))
_CLIENT_OVERRIDE := workspace/.compose-overrides/docker-compose.client-$(_FOCUS_CLIENT).yml
_MONOSERVER_LAYER := $(if $(and $(_FOCUS_CLIENT),$(wildcard $(_CLIENT_OVERRIDE))),$(_CLIENT_OVERRIDE),docker-compose.monoserver.yml)
COMPOSEF := $(COMPOSE) $(_ENV_FILE_ARG) -f docker-compose.yml -f $(_MONOSERVER_LAYER)

PROFILE_ARGS := $(if $(strip $(PROFILE)),--profile $(PROFILE),)
CLIENT_ARGS := $(if $(strip $(CLIENT)),--client $(CLIENT),)
SERVICE_ARGS := $(if $(strip $(SERVICE)),--service $(SERVICE),)
TASK_ARGS := $(if $(strip $(TASK)),--task $(TASK),)
LINES_ARGS := $(if $(strip $(LINES)),--lines $(LINES),)
BLUEPRINT_ARGS := $(if $(strip $(BLUEPRINT)),--blueprint $(BLUEPRINT),)
SET_ARGS := $(foreach s,$(SET),--set $(s))
DEPLOY_MANIFEST_ARGS := $(if $(strip $(DEPLOY_MANIFEST)),--deploy-manifest $(DEPLOY_MANIFEST),)
PRIVATE_PATH_ARGS := $(if $(strip $(PRIVATE_PATH)),--private-path $(PRIVATE_PATH),)
OUTPUT_DIR_ARGS := $(if $(strip $(OUTPUT_DIR)),--output-dir $(OUTPUT_DIR),)
FORCE_ARGS := $(if $(strip $(FORCE)),--force,)
RESUME_ARGS := $(if $(strip $(RESUME)),--resume,)
WRAPPER_BIN_DIR ?= $(HOME)/.local/bin
DEV_SHIM_BIN_DIR ?= $(HOME)/.local/skillbox-shims
DEV_SHIM_BINS := npm pnpm yarn vite next astro

.PHONY: help bootstrap-env install-hooks render doctor acceptance runtime-render runtime-sync runtime-status runtime-skills runtime-skill-audit runtime-bootstrap runtime-up runtime-down runtime-restart runtime-logs onboard first-box context dev-sanity python-cov-xml wrappers-install dev-shims-install build up up-surfaces down shell logs pulse-start pulse-stop pulse-status swimmers-install swimmers-start swimmers-stop swimmers-restart swimmers-status swimmers-logs swimmers-runtime-status box-up box-down box-status box-list box-ssh box-profiles box-register box-unregister

help:
	@printf "  make bootstrap-env  Seed .skillbox-state/operator/.env from .env.example if missing\n"
	@printf "  make install-hooks  Configure repo-local git hooks\n"
	@printf "  make render         Print the resolved sandbox model\n"
	@printf "  make doctor         Validate outer manifests, compose drift, and default skill-repo-set sync\n"
	@printf "  make acceptance     Run first-box acceptance for CLIENT=id (optional PROFILE=name)\n"
	@printf "  make runtime-render Print the resolved internal runtime graph (optional CLIENT=name PROFILE=name)\n"
	@printf "  make runtime-sync   Create managed repo/log dirs and install default skills (optional CLIENT=name PROFILE=name)\n"
	@printf "  make runtime-status Summarize repo/skill/service/log state (optional CLIENT=name PROFILE=name)\n"
	@printf "  make runtime-skills Show effective skills and global/project drift (optional CLIENT=name PROFILE=name)\n"
	@printf "  make runtime-skill-audit Audit skill policy across downstream repos (optional CLIENT=name PROFILE=name)\n"
	@printf "  make runtime-bootstrap Sync runtime state and run bootstrap tasks (optional CLIENT=name PROFILE=name TASK=id)\n"
	@printf "  make runtime-up     Sync runtime state and start manageable services (optional CLIENT=name PROFILE=name SERVICE=id)\n"
	@printf "  make runtime-down   Stop manageable services (optional CLIENT=name PROFILE=name SERVICE=id)\n"
	@printf "  make runtime-restart Restart manageable services (optional CLIENT=name PROFILE=name SERVICE=id)\n"
	@printf "  make runtime-logs   Show recent service logs (optional CLIENT=name PROFILE=name SERVICE=id LINES=n)\n"
	@printf "  make onboard        Scaffold, sync, bootstrap, start, context, verify in one step (CLIENT=id BLUEPRINT=name SET='K=V')\n"
	@printf "  make first-box      Attach the private repo, reuse or scaffold CLIENT, run acceptance, and open sand/CLIENT (defaults CLIENT=personal)\n"
	@printf "  make context        Generate CLAUDE.md and AGENTS.md from the runtime graph (optional CLIENT=name PROFILE=name)\n"
	@printf "  make pulse-start    Start the pulse reconciliation daemon (auto-heals crashed services)\n"
	@printf "  make pulse-stop     Stop the pulse daemon\n"
	@printf "  make pulse-status   Show pulse daemon status, supervised services, and recent heals\n"
	@printf "  make dev-sanity     Validate runtime graph, paths, and skill integrity (optional CLIENT=name PROFILE=name)\n"
	@printf "  make wrappers-install Install sbp/sbo symlinks into WRAPPER_BIN_DIR (default ~/.local/bin)\n"
	@printf "  make dev-shims-install Install dev-command guard shims into DEV_SHIM_BIN_DIR\n"
	@printf "  make build          Build the workspace image\n"
	@printf "  make up             Start the workspace container\n"
	@printf "  make up-surfaces    Start optional api and web stubs\n"
	@printf "  make down           Stop all containers\n"
	@printf "  make shell          Open a shell in the workspace container\n"
	@printf "  make logs           Tail compose logs\n"
	@printf "  make swimmers-install        Install the swimmers binary inside the workspace container\n"
	@printf "  make swimmers-start          Start swimmers inside the workspace container with the swimmers compose overlay\n"
	@printf "  make swimmers-stop           Stop the managed swimmers process inside the workspace container\n"
	@printf "  make swimmers-restart        Restart the managed swimmers process inside the workspace container\n"
	@printf "  make swimmers-status         Report swimmers workspace-local process and probe state\n"
	@printf "  make swimmers-logs           Tail swimmers server logs inside the workspace container\n"
	@printf "  make swimmers-runtime-status Summarize the runtime-manager swimmers overlay state\n"
	@printf "  make box-up         Create a DO+Tailscale box (BOX=id PROFILE=dev-small DEPLOY_MANIFEST=path; default BLUEPRINT=SPAPS auth)\n"
	@printf "  make box-down       Drain and destroy a box (BOX=id)\n"
	@printf "  make box-status     Check health of a box (BOX=id, omit for all)\n"
	@printf "  make box-list       List all active boxes\n"
	@printf "  make box-ssh        SSH into a box (BOX=id)\n"
	@printf "  make box-profiles   List available box profiles\n"
	@printf "  make box-register   Register an existing shared box locally (BOX=id HOST=name SSH_USER=user)\n"
	@printf "  make box-unregister Remove a registered shared box from local inventory (BOX=id)\n"

bootstrap-env: install-hooks
	@mkdir -p $(_STATE_ROOT)/operator
	@test -f $(_STATE_ROOT)/operator/.env || test -f ./.env || cp .env.example $(_STATE_ROOT)/operator/.env

install-hooks:
	@if git rev-parse --git-dir >/dev/null 2>&1; then \
		chmod +x .githooks/pre-commit; \
		git config core.hooksPath .githooks; \
	fi

render:
	@python3 scripts/04-reconcile.py render

doctor:
	@python3 scripts/04-reconcile.py doctor

acceptance:
	@python3 .env-manager/manage.py acceptance $(CLIENT) $(PROFILE_ARGS) --format json

runtime-render:
	@python3 .env-manager/manage.py render $(CLIENT_ARGS) $(PROFILE_ARGS)

runtime-sync:
	@python3 .env-manager/manage.py sync $(CLIENT_ARGS) $(PROFILE_ARGS)

runtime-status:
	@python3 .env-manager/manage.py status $(CLIENT_ARGS) $(PROFILE_ARGS)

runtime-skills:
	@python3 .env-manager/manage.py skills $(CLIENT_ARGS) $(PROFILE_ARGS)

runtime-skill-audit:
	@python3 .env-manager/manage.py skill-audit $(CLIENT_ARGS) $(PROFILE_ARGS)

runtime-bootstrap:
	@python3 .env-manager/manage.py bootstrap $(CLIENT_ARGS) $(PROFILE_ARGS) $(TASK_ARGS)

runtime-up:
	@python3 .env-manager/manage.py up $(CLIENT_ARGS) $(PROFILE_ARGS) $(SERVICE_ARGS)

runtime-down:
	@python3 .env-manager/manage.py down $(CLIENT_ARGS) $(PROFILE_ARGS) $(SERVICE_ARGS)

runtime-restart:
	@python3 .env-manager/manage.py restart $(CLIENT_ARGS) $(PROFILE_ARGS) $(SERVICE_ARGS)

runtime-logs:
	@python3 .env-manager/manage.py logs $(CLIENT_ARGS) $(PROFILE_ARGS) $(SERVICE_ARGS) $(LINES_ARGS)

onboard:
	@python3 .env-manager/manage.py onboard $(CLIENT) $(BLUEPRINT_ARGS) $(SET_ARGS)

first-box:
	@python3 .env-manager/manage.py first-box $(if $(strip $(CLIENT)),$(CLIENT),personal) $(PROFILE_ARGS) $(PRIVATE_PATH_ARGS) $(OUTPUT_DIR_ARGS) $(BLUEPRINT_ARGS) $(SET_ARGS) $(FORCE_ARGS)

context:
	@python3 .env-manager/manage.py context $(CLIENT_ARGS) $(PROFILE_ARGS)

dev-sanity:
	@python3 .env-manager/manage.py doctor $(CLIENT_ARGS) $(PROFILE_ARGS)

python-cov-xml:
	@python3 -m coverage erase
	@python3 -m coverage run --source=scripts,.env-manager -m unittest discover -s tests
	@python3 -m coverage xml -o coverage.xml
	@python3 -m coverage report -m --skip-covered

wrappers-install:
	@mkdir -p "$(WRAPPER_BIN_DIR)"
	@ln -sf "$(CURDIR)/scripts/sbp" "$(WRAPPER_BIN_DIR)/sbp"
	@ln -sf "$(CURDIR)/scripts/sbo" "$(WRAPPER_BIN_DIR)/sbo"
	@chmod +x "$(CURDIR)/scripts/sbp" "$(CURDIR)/scripts/sbo"
	@SKILLBOX_ROOT="$(CURDIR)" "$(WRAPPER_BIN_DIR)/sbp" --help >/dev/null
	@SKILLBOX_ROOT="$(CURDIR)" "$(WRAPPER_BIN_DIR)/sbo" --help >/dev/null
	@printf "installed wrappers: %s/sbp %s/sbo\n" "$(WRAPPER_BIN_DIR)" "$(WRAPPER_BIN_DIR)"
	@$(MAKE) --no-print-directory dev-shims-install

dev-shims-install:
	@mkdir -p "$(DEV_SHIM_BIN_DIR)"
	@chmod +x "$(CURDIR)/scripts/guard-dev-port.sh" "$(CURDIR)/scripts/skillbox-dev-shim.sh"
	@for bin in $(DEV_SHIM_BINS); do \
		ln -sf "$(CURDIR)/scripts/skillbox-dev-shim.sh" "$(DEV_SHIM_BIN_DIR)/$$bin"; \
	done
	@printf "installed dev shims: %s (%s)\n" "$(DEV_SHIM_BIN_DIR)" "$(DEV_SHIM_BINS)"

pulse-start:
	@python3 .env-manager/pulse.py run &

pulse-stop:
	@python3 .env-manager/pulse.py stop

pulse-status:
	@python3 .env-manager/pulse.py status

build: bootstrap-env
	@$(COMPOSEF) build

up: bootstrap-env
	@$(COMPOSEF) up -d workspace

up-surfaces: bootstrap-env
	@$(COMPOSEF) --profile surfaces up -d api web

down:
	@$(COMPOSEF) down

shell: bootstrap-env
	@$(COMPOSEF) exec workspace zsh

logs:
	@$(COMPOSEF) logs -f --tail=200

swimmers-install: bootstrap-env
	@./scripts/05-swimmers.sh install

swimmers-start: bootstrap-env
	@./scripts/05-swimmers.sh start

swimmers-stop: bootstrap-env
	@./scripts/05-swimmers.sh stop

swimmers-restart: bootstrap-env
	@./scripts/05-swimmers.sh restart

swimmers-status: bootstrap-env
	@./scripts/05-swimmers.sh status

swimmers-logs: bootstrap-env
	@./scripts/05-swimmers.sh logs

swimmers-runtime-status:
	@python3 .env-manager/manage.py status --profile swimmers $(CLIENT_ARGS)

BOX_ARGS := $(if $(strip $(BOX)),$(BOX),)

box-up:
	@python3 scripts/box.py up $(BOX_ARGS) --profile $(or $(PROFILE),dev-small) $(DEPLOY_MANIFEST_ARGS) $(BLUEPRINT_ARGS) $(SET_ARGS) $(RESUME_ARGS)

box-down:
	@python3 scripts/box.py down $(BOX_ARGS)

box-status:
	@python3 scripts/box.py status $(BOX_ARGS)

box-list:
	@python3 scripts/box.py list

box-ssh:
	@python3 scripts/box.py ssh $(BOX_ARGS)

box-profiles:
	@python3 scripts/box.py profiles

HOST_ARGS := $(if $(strip $(HOST)),--host $(HOST),)
SSH_USER_ARGS := $(if $(strip $(SSH_USER)),--ssh-user $(SSH_USER),)
NO_PROBE_ARGS := $(if $(strip $(NO_PROBE)),--no-probe,)

box-register:
	@python3 scripts/box.py register $(BOX_ARGS) $(HOST_ARGS) $(PROFILE_ARGS) $(SSH_USER_ARGS) $(FORCE_ARGS) $(NO_PROBE_ARGS)

box-unregister:
	@python3 scripts/box.py unregister $(BOX_ARGS)
