"""Standalone checks for config_model's PinConfig equality — no hardware,
pywebview, or serial connection needed. No test framework is installed in
this project yet, so these are plain functions with asserts, each runnable
directly and also auto-discoverable if pytest is added later.

Run with: python3 app/tests/test_config_model.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config_model


def test_equal_ignores_fields_outside_to_tuple():
    # `pin` itself is deliberately excluded from to_tuple() (it's the list
    # position, not part of the stored 12-byte record), so it's today's
    # concrete example of a field equal() must ignore. This is also the
    # regression this test guards against: equal() used to compare the full
    # dataclass tuple, which would treat differing `pin` as a real
    # mismatch, and will do the same for any future app-only field (e.g. a
    # value-mirror target pin) that's deliberately kept out of to_tuple().
    a = config_model.PinConfig(pin=10, role="encoder_a", channel=2, number=30)
    b = config_model.PinConfig(pin=11, role="encoder_a", channel=2, number=30)
    assert config_model.PinConfig.equal(a, b), "a field excluded from to_tuple() must not affect equality"


def test_equal_detects_blob_field_differences():
    a = config_model.PinConfig(pin=10, role="encoder_a", channel=2, number=30)
    b = config_model.PinConfig(pin=10, role="encoder_a", channel=2, number=31)
    assert not config_model.PinConfig.equal(a, b), "a differing blob field must compare unequal"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    if failures:
        print(f"\n{failures}/{len(tests)} failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} checks passed")
