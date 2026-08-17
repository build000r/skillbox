.PHONY: test-unit test-integration lint

lint:
	ruff check .

test-unit:
	pytest -q tests/unit --junitxml=reports/unit.xml

test-integration:
	pytest -q tests/integration --junitxml=reports/integration.xml
