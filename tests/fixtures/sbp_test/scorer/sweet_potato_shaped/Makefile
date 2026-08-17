.PHONY: test test-unit deps-up deps-down

deps-up:
	./scripts/orb_test_dependencies.sh up

test:
	flock .deps.lock pytest -n auto --junitxml=reports/pytest.xml

test-unit:
	flock .deps.lock pytest -n auto -q tests/unit
