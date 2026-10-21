"""Placeholder for the private native SQLite component.

The open-source build ships only the fixture CAS profile. Any request for the
native component is rejected (fail-closed).
"""
SCHEMA = object()  # never equal to any configured string


def _unavailable(*_a, **_k):
    raise RuntimeError("native_component_not_included")


fixture_path = verify = statements = _unavailable
