.PHONY: self-test contract render unittest

contract:
	python3 scripts/contract_check.py --junitxml=reports/contract.xml

render:
	python3 scripts/render_check.py --junitxml=reports/render.xml

unittest:
	python3 -m unittest discover -s tests

self-test: contract render unittest
