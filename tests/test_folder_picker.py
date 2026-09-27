import subprocess

from mediavault.web import app as web_app


def test_pick_folder_returns_path_on_success(monkeypatch):
    def fake_run(*args, **kwargs):
        assert kwargs.get("timeout"), "must bound how long a picker dialog can block the request"
        return subprocess.CompletedProcess(args, 0, stdout="/Volumes/Backup/Movies\n", stderr="")

    monkeypatch.setattr(web_app.subprocess, "run", fake_run)
    monkeypatch.setattr(web_app.platform, "system", lambda: "Darwin")

    response = web_app.roots_pick_folder()
    assert response.status_code == 200
    assert response.body == b'{"path":"/Volumes/Backup/Movies"}'


def test_pick_folder_handles_user_cancel(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="execution error: User canceled. (-128)")

    monkeypatch.setattr(web_app.subprocess, "run", fake_run)
    monkeypatch.setattr(web_app.platform, "system", lambda: "Darwin")

    response = web_app.roots_pick_folder()
    import json

    body = json.loads(response.body)
    assert body["path"] is None
    assert body["canceled"] is True


def test_pick_folder_handles_hung_dialog_via_timeout(monkeypatch):
    """A picker dialog left open indefinitely (or the tab closed mid-pick)
    must never hang the request forever — this is the exact bug caught while
    manually testing: an abandoned Finder dialog stayed open with no bound."""

    def fake_run(*args, **kwargs):
        assert kwargs.get("timeout"), "must pass a timeout so subprocess kills the dialog itself"
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    monkeypatch.setattr(web_app.subprocess, "run", fake_run)
    monkeypatch.setattr(web_app.platform, "system", lambda: "Darwin")

    response = web_app.roots_pick_folder()
    import json

    body = json.loads(response.body)
    assert body["path"] is None
    assert body["canceled"] is True


def test_pick_folder_no_picker_available_on_unsupported_platform(monkeypatch):
    monkeypatch.setattr(web_app.platform, "system", lambda: "Windows")

    response = web_app.roots_pick_folder()
    assert response.status_code == 501
