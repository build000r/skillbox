"""Service markers are auto-derived from fixture requests, as the exemplar does."""


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "shared_postgres_url" in getattr(item, "fixturenames", ()):
            item.add_marker("db")
