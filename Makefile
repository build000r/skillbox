COMPOSE := docker compose

.PHONY: help bootstrap-env render doctor runtime-render runtime-sync runtime-status dev-sanity build up up-surfaces down shell logs

help:
	@printf "  make bootstrap-env  Copy .env.example to .env if missing\n"
	@printf "  make render         Print the resolved sandbox model\n"
	@printf "  make doctor         Validate manifest/runtime drift\n"
	@printf "  make runtime-render Print the resolved internal runtime graph\n"
	@printf "  make runtime-sync   Create managed repo/log directories\n"
	@printf "  make runtime-status Summarize repo/service/log state\n"
	@printf "  make dev-sanity     Validate internal runtime graph and paths\n"
	@printf "  make build          Build the workspace image\n"
	@printf "  make up             Start the workspace container\n"
	@printf "  make up-surfaces    Start optional api and web stubs\n"
	@printf "  make down           Stop all containers\n"
	@printf "  make shell          Open a shell in the workspace container\n"
	@printf "  make logs           Tail compose logs\n"

bootstrap-env:
	@test -f .env || cp .env.example .env

render:
	@python3 scripts/04-reconcile.py render

doctor:
	@python3 scripts/04-reconcile.py doctor

runtime-render:
	@python3 .env-manager/manage.py render

runtime-sync:
	@python3 .env-manager/manage.py sync

runtime-status:
	@python3 .env-manager/manage.py status

dev-sanity:
	@python3 .env-manager/manage.py doctor

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
