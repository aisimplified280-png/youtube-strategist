import dashboard_security


SECRET = "test-only-secret-with-more-than-32-characters"
SUBJECT = "google-subject-123"
NOW = 1_800_000_000


def test_oauth_state_accepts_matching_session_state():
    state = dashboard_security.create_oauth_state(SUBJECT, SECRET, now=NOW)
    assert dashboard_security.consume_oauth_state(
        state, state, SUBJECT, SECRET, now=NOW + 1
    )


def test_oauth_state_accepts_signed_state_when_streamlit_session_was_lost():
    state = dashboard_security.create_oauth_state(SUBJECT, SECRET, now=NOW)
    assert dashboard_security.consume_oauth_state(
        state, None, SUBJECT, SECRET, now=NOW + 1
    )


def test_oauth_state_rejects_mismatch_when_expected_state_exists():
    state = dashboard_security.create_oauth_state(SUBJECT, SECRET, now=NOW)
    other_state = dashboard_security.create_oauth_state(
        SUBJECT, SECRET, now=NOW, nonce="different-nonce"
    )
    assert not dashboard_security.consume_oauth_state(
        state, other_state, SUBJECT, SECRET, now=NOW + 1
    )


def test_oauth_state_rejects_different_google_subject():
    state = dashboard_security.create_oauth_state(SUBJECT, SECRET, now=NOW)
    assert not dashboard_security.consume_oauth_state(
        state, None, "different-google-subject", SECRET, now=NOW + 1
    )


def test_oauth_state_rejects_expired_state_without_session_state():
    state = dashboard_security.create_oauth_state(SUBJECT, SECRET, now=NOW)
    assert not dashboard_security.consume_oauth_state(
        state,
        None,
        SUBJECT,
        SECRET,
        now=NOW + dashboard_security.OAUTH_STATE_TTL_SECONDS + 1,
    )


def test_oauth_state_rejects_tampering_without_session_state():
    state = dashboard_security.create_oauth_state(SUBJECT, SECRET, now=NOW)
    tampered = state[:-1] + ("0" if state[-1] != "0" else "1")
    assert not dashboard_security.consume_oauth_state(
        tampered, None, SUBJECT, SECRET, now=NOW + 1
    )
