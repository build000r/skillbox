.PHONY: test

test:
	flock /tmp/suite.lock docker compose up -d && cd /Users/someone/checkout && pytest && ruff check .
