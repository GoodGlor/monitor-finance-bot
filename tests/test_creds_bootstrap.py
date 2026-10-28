import json
import os

from creds_bootstrap import materialize_credentials


def test_writes_both_files_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('GOOGLE_CREDENTIALS_JSON', json.dumps({"installed": {"client_id": "x"}}))
    monkeypatch.setenv('GOOGLE_TOKEN_JSON', json.dumps({"refresh_token": "y"}))

    written = materialize_credentials(str(tmp_path / "creds"))

    assert sorted(written) == ["credentials.json", "token.json"]
    assert json.loads((tmp_path / "creds" / "token.json").read_text())["refresh_token"] == "y"


def test_creates_the_directory_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv('GOOGLE_TOKEN_JSON', '{"refresh_token": "y"}')
    target = tmp_path / "nested" / "creds"
    materialize_credentials(str(target))
    assert (target / "token.json").exists()


def test_does_nothing_without_the_variables(tmp_path, monkeypatch):
    monkeypatch.delenv('GOOGLE_CREDENTIALS_JSON', raising=False)
    monkeypatch.delenv('GOOGLE_TOKEN_JSON', raising=False)
    assert materialize_credentials(str(tmp_path / "creds")) == []


def test_never_overwrites_an_existing_file(tmp_path, monkeypatch):
    # On a laptop the real token is being actively refreshed; a stale config
    # var must not clobber it.
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "token.json").write_text('{"refresh_token": "on-disk"}')
    monkeypatch.setenv('GOOGLE_TOKEN_JSON', '{"refresh_token": "from-env"}')

    assert materialize_credentials(str(creds)) == []
    assert json.loads((creds / "token.json").read_text())["refresh_token"] == "on-disk"


def test_rejects_invalid_json_rather_than_writing_a_broken_file(tmp_path, monkeypatch):
    # A truncated paste into a config var must fail here, not deep inside the
    # OAuth library with an unrelated message.
    monkeypatch.setenv('GOOGLE_TOKEN_JSON', '{"refresh_token": ')
    assert materialize_credentials(str(tmp_path / "creds")) == []
    assert not (tmp_path / "creds" / "token.json").exists()


def test_does_not_log_the_secret(tmp_path, monkeypatch, caplog):
    secret = "1//0gTHIS-IS-A-REFRESH-TOKEN"
    monkeypatch.setenv('GOOGLE_TOKEN_JSON', json.dumps({"refresh_token": secret}))
    with caplog.at_level('DEBUG'):
        materialize_credentials(str(tmp_path / "creds"))
    assert secret not in caplog.text


def test_invalid_json_is_not_echoed_into_the_log(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv('GOOGLE_TOKEN_JSON', '{"refresh_token": "1//0gLEAKED')
    with caplog.at_level('DEBUG'):
        materialize_credentials(str(tmp_path / "creds"))
    assert "1//0gLEAKED" not in caplog.text


def test_headless_login_raises_instead_of_blocking(monkeypatch, tmp_path):
    # The failure this exists to prevent: run_local_server() waits forever for
    # a browser redirect that cannot arrive, wedging the run (and, under
    # flock, every run after it).
    import auth_service

    monkeypatch.setattr(auth_service.sys.stdin, 'isatty', lambda: False, raising=False)
    monkeypatch.setattr(auth_service.AuthService, '_interactive_login_possible',
                        staticmethod(lambda: False))

    def explode(*a, **k):
        raise AssertionError('run_local_server must never be reached headless')

    monkeypatch.setattr(auth_service.InstalledAppFlow, 'from_client_secrets_file', explode)

    svc = auth_service.AuthService.__new__(auth_service.AuthService)
    svc._prj_path = str(tmp_path)
    svc._scopes = ('scope',)

    try:
        svc._login()
    except RuntimeError as error:
        assert 'no browser is available' in str(error)
    else:
        raise AssertionError('expected a RuntimeError')
