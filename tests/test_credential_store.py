from cryptography.fernet import Fernet

from camviewer.account_state import AccountState
from camviewer.credential_store import CredentialStore


def test_roundtrip_and_ciphertext_has_no_plaintext(tmp_path):
    store = CredentialStore(tmp_path, Fernet.generate_key().decode())
    store.save("tee@example.com", "hunter2-password")
    raw = (tmp_path / "credentials.enc").read_bytes()
    assert b"hunter2-password" not in raw and b"tee@example.com" not in raw
    assert store.load() == ("tee@example.com", "hunter2-password")
    assert store.username() == "tee@example.com"


def test_passphrase_secret_and_wrong_key(tmp_path):
    CredentialStore(tmp_path, "any passphrase").save("u", "p")
    assert CredentialStore(tmp_path, "any passphrase").load() == ("u", "p")
    assert CredentialStore(tmp_path, "other").load() is None


def test_forget_and_missing_file(tmp_path):
    store = CredentialStore(tmp_path, "k")
    assert store.load() is None
    store.save("u", "p")
    store.forget()
    store.forget()
    assert store.load() is None
    assert not list(tmp_path.glob("*.tmp"))


def test_epoch_persists_and_bumps(tmp_path):
    state = AccountState(tmp_path)
    assert state.epoch == 1
    state.bump_epoch()
    assert AccountState(tmp_path).epoch == 2


def test_breaker_trips_after_three_failures(tmp_path):
    state = AccountState(tmp_path)
    state.mark_ok()
    assert [state.record_auth_failure() for _ in range(3)] == [False, False, True]
    assert state.status == "needs_login" and not state.usable
