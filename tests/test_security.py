"""Remote-access safety: password guessing lockout, cross-site requests, headers, Tailscale detection."""
import json
import os
import stat
import sys

import pytest
from fastapi.testclient import TestClient

from liverhu import remote
from liverhu.web import auth
from liverhu.web.app import LoginGuard, create_app

PW = "correct-horse-1"


@pytest.fixture()
def client(tmp_path):
    auth.set_password(tmp_path, PW)
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as c:
        yield c, app


def test_guessing_passwords_locks_the_sender_out(client):
    c, app = client
    app.state.login_guard.max_wrong = 5
    for i in range(4):
        assert c.get("/", auth=("x", f"guess-{i}")).status_code == 401
    assert c.get("/", auth=("x", PW)).status_code == 200          # a right password still works...
    for i in range(5):
        c.get("/", auth=("x", f"other-{i}"))
    r = c.get("/", auth=("x", PW))                                # ...but not after 5 different guesses
    assert r.status_code == 429 and "Too many wrong passwords" in r.text and "Retry-After" in r.headers


def test_same_stale_password_does_not_lock_out(client):
    c, app = client
    app.state.login_guard.max_wrong = 3
    for _ in range(20):                       # e.g. an open tab still polling with an old password
        assert c.get("/api/jobs", auth=("x", "old-password-1")).status_code == 401
    assert c.get("/", auth=("x", PW)).status_code == 200


def test_people_behind_tailscale_are_told_apart():
    class Req:
        def __init__(self, host, headers):
            self.client = type("C", (), {"host": host})()
            self.headers = headers
    assert LoginGuard.sender(Req("127.0.0.1", {"tailscale-user-login": "ana@lab.org"})) == "via ana@lab.org"
    assert LoginGuard.sender(Req("127.0.0.1", {"x-forwarded-for": "100.64.1.2, 10.0.0.1"})) == "via 100.64.1.2"
    assert LoginGuard.sender(Req("127.0.0.1", {})) == "127.0.0.1"
    # a header sent straight from another computer is not trusted
    assert LoginGuard.sender(Req("192.168.1.9", {"tailscale-user-login": "fake"})) == "192.168.1.9"


def test_changes_from_other_websites_are_refused(client):
    c, _ = client
    a = ("maria", PW)
    r = c.post("/api/jobs", data={"label": "x"}, auth=a, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert c.post("/api/jobs", data={"label": "x"}, auth=a, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert c.get("/", auth=a, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200   # links still open


def test_security_headers_and_no_caching_of_results(client):
    c, _ = client
    r = c.get("/", auth=("maria", PW))
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    assert c.get("/api/jobs", auth=("maria", PW)).headers["cache-control"] == "no-store"
    s = c.get("/static/style.css")
    assert s.status_code == 200 and s.headers.get("cache-control") != "no-store"
    assert s.headers["x-content-type-options"] == "nosniff"


def _fake_tailscale(tmp_path, status: dict | None, serve: dict):
    """A stand-in `tailscale` command that prints the given JSON."""
    p = tmp_path / "tailscale"
    p.write_text("#!" + sys.executable + "\nimport sys, json\n"
                 f"st = {json.dumps(status)!r}\nsv = {json.dumps(serve)!r}\n"
                 "a = sys.argv[1:]\n"
                 "if a[:1] == ['status']:\n    print(st) if st != 'null' else sys.exit(1)\n"
                 "elif a[:2] == ['serve', 'status']:\n    print(sv)\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


@pytest.mark.skipif(os.name == "nt", reason="uses a script with a shebang")
def test_tailscale_detection(tmp_path, monkeypatch):
    up = {"BackendState": "Running", "Self": {"DNSName": "lab-mac.tail1234.ts.net."}}
    serving = {"Web": {"lab-mac.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}}}}
    monkeypatch.setenv("LIVERHU_TAILSCALE", _fake_tailscale(tmp_path, up, serving))
    assert remote.tailscale_state(8000) == {"state": "serving", "url": "https://lab-mac.tail1234.ts.net"}
    assert remote.tailscale_state(9000)["state"] == "not_serving"
    monkeypatch.setenv("LIVERHU_TAILSCALE", _fake_tailscale(tmp_path, {"BackendState": "Stopped"}, {}))
    assert remote.tailscale_state(8000)["state"] == "not_connected"
    monkeypatch.setenv("LIVERHU_TAILSCALE", str(tmp_path / "missing"))
    assert remote.tailscale_state(8000)["state"] == "not_installed"


def test_address_lines_show_encrypted_addresses_only(monkeypatch):
    from liverhu import cli
    from liverhu.web import tls
    monkeypatch.setattr(tls, "local_ips", lambda: ["192.168.4.208", "100.101.1.2"])
    monkeypatch.setattr(remote, "tailscale_state", lambda port: {"state": "serving", "url": "https://m.t.ts.net"})
    text = "\n".join(cli._address_lines(8000, 8443))
    assert "http://localhost:8000" in text and "https://192.168.4.208:8443" in text
    assert "http://192.168.4.208:8000" not in text            # no unencrypted address for others
    assert "100.101.1.2" not in text and "https://m.t.ts.net" in text
    assert "NOT encrypted" in "\n".join(cli._address_lines(8000, 8443, lan_http=True))


def test_waiting_scans_survive_moving_the_folder(tmp_path):
    from liverhu.web.jobs import relocate_case_file
    old = tmp_path / "Downloads" / "REPO--main" / "liverhu_data"
    new = tmp_path / "HUnter" / "liverhu_data"
    d = new / "jobs" / "3" / "cases" / "7"
    d.mkdir(parents=True)
    old_d = old / "jobs" / "3" / "cases" / "7"
    up = str(old / "jobs" / "3" / "upload")
    (d / "case.json").write_text(json.dumps({"case": {"path": up + "/a.nii.gz", "files": [up + "/s/1", up + "/s/2"]},
                                             "settings": {}, "out_dir": str(old_d)}))
    relocate_case_file(d)
    job = json.loads((d / "case.json").read_text())
    nup = str(new / "jobs" / "3" / "upload")
    assert job["out_dir"] == str(d)
    assert job["case"]["path"] == nup + "/a.nii.gz" and job["case"]["files"] == [nup + "/s/1", nup + "/s/2"]
    relocate_case_file(d)                                  # already right: unchanged
    assert json.loads((d / "case.json").read_text()) == job
