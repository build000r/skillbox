include common.mk

REVISION := $(shell git rev-parse HEAD)
SOURCES != find . -name '*.py'

%.checked: %.py
	python3 -m py_compile $<

.PHONY: test
test: $(SOURCES:.py=.checked)
	$(MAKE) -C subproject test
