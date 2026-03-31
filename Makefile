COMPOSE := docker compose
PROFILE_ARGS := $(if $(strip $(PROFILE)),--profile $(PROFILE),)
CLIENT_ARGS := $(if $(strip $(CLIENT)),--client $(CLIENT),)
SERVICE_ARGS := $(if $(strip $(SERVICE)),--service $(SERVICE),)
TASK_ARGS := $(if $(strip $(TASK)),--task $(TASK),)
LINES_ARGS := $(if $(strip $(LINES)),--lines $(LINES),)
BLUEPRINT_ARGS := $(if $(strip $(BLUEPRINT)),--blueprint $(BLUEPRINT),)
SET_ARGS := $(foreach s,$(SET),--set $(s))

.PHONY: help bootstrap-env render doctor runtime-render runtime-sync runtime-status runtime-bootstrap runtime-up runtime-down runtime-restart runtime-logs onboard context dev-sanity build up up-surfaces down shell logs swimmers-install swimmers-start swimmers-stop swimmers-restart swimmers-status swimmers-logs swimmers-runtime-status box-up box-down box-status box-list box-ssh box-profiles

help:
	@printf "  make bootstrap-env  Copy .env.example to .env if missing\n"
	@printf "  make render         Print the resolved sandbox model\n"
	@printf "  make doctor         Validate manifest/runtime drift\n"
	@printf "  make runtime-render Print the resolved internal runtime graph (optional CLIENT=name PROFILE=name)\n"
	@printf "  make runtime-sync   Create managed repo/log dirs and install default skills (optional CLIENT=name PROFILE=name)\n"
	@printf "  make runtime-status Summarize repo/skill/service/log state (optional CLIENT=name PROFILE=name)\n"
	@printf "  make runtime-bootstrap Sync runtime state and run bootstrap tasks (optional CLIENT=name PROFILE=name TASK=id)\n"
	@printf "  make runtime-up     Sync runtime state and start manageable services (optional CLIENT=name PROFILE=name SERVICE=id)\n"
	@printf "  make runtime-down   Stop manageable services (optional CLIENT=name PROFILE=name SERVICE=id)\n"
	@printf "  make runtime-restart Restart manageable services (optional CLIENT=name PROFILE=name SERVICE=id)\n"
	@printf "  make runtime-logs   Show recent service logs (optional CLIENT=name PROFILE=name SERVICE=id LINES=n)\n"
	@printf "  make onboard        Scaffold, sync, bootstrap, start, context, verify in one step (CLIENT=id BLUEPRINT=name SET='K=V')\n"
	@printf "  make context        Generate CLAUDE.md and AGENTS.md from the runtime graph (optional CLIENT=name PROFILE=name)\n"
	@printf "  make dev-sanity     Validate runtime graph, paths, and skill integrity (optional CLIENT=name PROFILE=name)\n"
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
	@printf "  make box-up         Create a DO+Tailscale box (BOX=id PROFILE=dev-small BLUEPRINT=name SET='K=V')\n"
	@printf "  make box-down       Drain and destroy a box (BOX=id)\n"
	@printf "  make box-status     Check health of a box (BOX=id, omit for all)\n"
	@printf "  make box-list       List all active boxes\n"
	@printf "  make box-ssh        SSH into a box (BOX=id)\n"
	@printf "  make box-profiles   List available box profiles\n"

bootstrap-env:
	@test -f .env || cp .env.example .env

render:
	@python3 scripts/04-reconcile.py render

doctor:
	@python3 scripts/04-reconcile.py doctor

runtime-render:
	@python3 .env-manager/manage.py render $(CLIENT_ARGS) $(PROFILE_ARGS)

runtime-sync:
	@python3 .env-manager/manage.py sync $(CLIENT_ARGS) $(PROFILE_ARGS)

runtime-status:
	@python3 .env-manager/manage.py status $(CLIENT_ARGS) $(PROFILE_ARGS)

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

context:
	@python3 .env-manager/manage.py context $(CLIENT_ARGS) $(PROFILE_ARGS)

dev-sanity:
	@python3 .env-manager/manage.py doctor $(CLIENT_ARGS) $(PROFILE_ARGS)

build: bootstrap-env
	@$(COMPOSE) build

up: bootstrap-env
	@$(COMPOSE) up -d workspace

up-surfaces: bootstrap-env
	@$(COMPOSE) --profile surfaces up -d api web

down:
	@$(COMPOSE) down

shell: bootstrap-env
	@$(COMPOSE) exec workspace zsh

logs:
	@$(COMPOSE) logs -f --tail=200

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
	@python3 scripts/box.py up $(BOX_ARGS) --profile $(or $(PROFILE),dev-small) $(BLUEPRINT_ARGS) $(SET_ARGS)

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
