"""Markers are derived from what a test actually requests, never hand-applied."""


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "postgres" in getattr(item, "fixturenames", ()):
            item.add_marker("db")
