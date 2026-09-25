from debugbundle.suppression import MAX_TRACKED_FINGERPRINTS, EventSuppressionTracker


def test_unique_burst_has_a_bounded_privacy_safe_fingerprint_table() -> None:
    tracker = EventSuppressionTracker()
    for index in range(MAX_TRACKED_FINGERPRINTS + 500):
        assert tracker.should_capture(f"Authorization: Bearer SECRET_{index}", 100.0)
    assert len(tracker._states) == MAX_TRACKED_FINGERPRINTS
    assert all(len(key) == 64 and "SECRET" not in key for key in tracker._states)
