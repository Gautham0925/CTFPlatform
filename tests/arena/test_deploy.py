"""Phase 15: deployment profiles.

Static checks always run. Compose validation runs when `docker compose` is
installed; the backup/restore round trip runs against MariaDB only
(TESTING_DATABASE_URL=mysql+pymysql://...), using the scripts' direct mode.
"""

import os
import pathlib
import re
import shutil
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
COMPOSE = DEPLOY / "compose"
PLATFORM_FILES = ["dev.yml", "community.yml", "competition.yml"]


def _load(name):
    return yaml.safe_load((COMPOSE / name).read_text())


def test_shell_scripts_parse():
    scripts = list((DEPLOY / "scripts").glob("*.sh")) + [
        DEPLOY / "docker" / "arena-entrypoint.sh"
    ]
    assert len(scripts) >= 9
    for s in scripts:
        subprocess.run(["bash", "-n", str(s)], check=True)
        assert os.access(s, os.X_OK) or s.name == "lib.sh", s


def test_platform_never_gets_docker_socket_or_privileges():
    for name in PLATFORM_FILES:
        text = (COMPOSE / name).read_text()
        assert "docker.sock" not in text, name
        assert "privileged" not in text, name
        for svc in (_load(name).get("services") or {}).values():
            assert "pid" not in svc and "network_mode" not in svc
    # The runner (separate host) is the only one with the socket.
    assert "docker.sock" in (COMPOSE / "runner.yml").read_text()


def test_only_edge_ports_published_in_profile_b():
    services = _load("community.yml")["services"]
    published = {name for name, s in services.items() if s.get("ports")}
    assert published == {"nginx"}
    assert _load("community.yml")["networks"]["internal"]["internal"] is True
    for name in ("db", "cache"):
        assert services[name]["networks"] == [
            "internal"
        ]  # never reachable from outside
    for name in ("ctfd", "worker", "migrate"):
        assert "edge" not in services[name]["networks"]
    # dev profile binds to loopback only
    assert _load("dev.yml")["services"]["ctfd"]["ports"] == ["127.0.0.1:8000:8000"]


def test_profile_b_requires_secrets_and_keeps_certificates_off():
    text = (COMPOSE / "community.yml").read_text()
    for var in ("MARIADB_PASSWORD", "REDIS_PASSWORD", "MARIADB_ROOT_PASSWORD"):
        assert "${" + var + ":?" in text, var  # compose refuses to start without
    example = (DEPLOY / "examples" / "platform.env.example").read_text()
    assert "ARENA_CERTIFICATES_ENABLED=false" in example
    for line in example.splitlines():
        m = re.match(
            r"^(SECRET_KEY|ARENA_SECRET_KEY|MARIADB_\w*PASSWORD|REDIS_PASSWORD)=(.*)$",
            line,
        )
        if m:
            assert m.group(2) == "CHANGE_ME", line  # no real secret committed


def test_secrets_and_runner_never_enter_platform_image():
    ignore = (DEPLOY / "docker" / "platform.Dockerfile.dockerignore").read_text()
    for pattern in ("runner", "deploy/.env", "deploy/nginx/certs", "**/.venv", "tests"):
        assert pattern in ignore.splitlines(), pattern
    gi = (DEPLOY / ".gitignore").read_text()
    for pattern in (".env", "nginx/certs/", "backups/", "compose/runner-tls/"):
        assert pattern in gi.splitlines(), pattern


def test_nginx_overwrites_forwarded_for_and_sets_tls_headers():
    conf = (DEPLOY / "nginx" / "arena.conf.template").read_text()
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in conf
    assert "$proxy_add_x_forwarded_for" not in conf  # would allow IP spoofing
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in conf
    assert "Strict-Transport-Security" in conf
    assert re.search(r"location ~ \^/api/v1/\(challenges/attempt\|arena/events", conf)
    assert "return 301 https://" in conf


def test_gen_secrets_fills_placeholders(tmp_path):
    env = tmp_path / ".env"
    shutil.copy(DEPLOY / "examples" / "platform.env.example", env)
    subprocess.run(
        [str(DEPLOY / "scripts" / "gen-secrets.sh"), str(env)],
        check=True,
        capture_output=True,
    )
    values = dict(
        line.split("=", 1)
        for line in env.read_text().splitlines()
        if line and not line.startswith("#")
    )
    secrets = [
        values[k]
        for k in (
            "SECRET_KEY",
            "ARENA_SECRET_KEY",
            "MARIADB_PASSWORD",
            "MARIADB_ROOT_PASSWORD",
            "REDIS_PASSWORD",
        )
    ]
    assert all(re.fullmatch(r"[A-Za-z0-9]{48}", s) for s in secrets)
    assert len(set(secrets)) == len(secrets)
    assert oct(env.stat().st_mode & 0o777) == "0o600"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_compose_files_validate(tmp_path):
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode:
        pytest.skip("docker compose v2 not installed")
    env = tmp_path / ".env"
    shutil.copy(DEPLOY / "examples" / "platform.env.example", env)
    subprocess.run(
        [str(DEPLOY / "scripts" / "gen-secrets.sh"), str(env)],
        check=True,
        capture_output=True,
    )
    base = ["docker", "compose", "--env-file", str(env)]
    run_env = {**os.environ, "ARENA_ENV_FILE": str(env)}
    for files in (["dev.yml"], ["community.yml"], ["community.yml", "competition.yml"]):
        args = sum((["-f", str(COMPOSE / f)] for f in files), [])
        r = subprocess.run(
            base + args + ["config", "--quiet"],
            capture_output=True,
            text=True,
            env=run_env,
        )
        assert r.returncode == 0, (files, r.stderr)
    # Without the secrets, Profile B must refuse to render.
    empty = tmp_path / "empty.env"
    empty.write_text("")
    r = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(empty),
            "-f",
            str(COMPOSE / "community.yml"),
            "config",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "ARENA_ENV_FILE": str(empty)},
    )
    assert r.returncode != 0 and "MARIADB_PASSWORD" in r.stderr


def _mysql_url():
    url = os.environ.get("TESTING_DATABASE_URL", "")
    m = re.match(r"mysql\+pymysql://([^:]+):([^@]+)@([^/:]+)(?::(\d+))?/(\w+)", url)
    return m


@pytest.mark.skipif(
    not _mysql_url(), reason="needs TESTING_DATABASE_URL=mysql+pymysql://"
)
@pytest.mark.skipif(
    shutil.which("mariadb-dump") is None and shutil.which("mysqldump") is None,
    reason="mariadb client tools not installed",
)
def test_backup_restore_round_trip_direct_mode(tmp_path):
    """backup.sh -> destructive change -> restore.sh restores data + uploads."""
    import pymysql
    from sqlalchemy.engine.url import make_url

    from tests.arena.helpers import create_arena_ctfd, gen_event, make_user
    from tests.helpers import destroy_ctfd

    user, password, host, port, _ = _mysql_url().groups()
    app = create_arena_ctfd()  # per-test database (tests.helpers uses a uuid name)
    dbname = make_url(app.config["SQLALCHEMY_DATABASE_URI"]).database
    url = app.config["SQLALCHEMY_DATABASE_URI"]
    try:
        with app.app_context():
            eid = gen_event("bk").id
            make_user("keeper")
        uploads = tmp_path / "uploads"
        (uploads / "abc").mkdir(parents=True)
        (uploads / "abc" / "f.txt").write_text("payload")

        def snapshot():
            conn = pymysql.connect(
                host=host,
                port=int(port or 3306),
                user=user,
                password=password,
                database=dbname,
            )
            with conn.cursor() as c:
                c.execute(
                    "CHECKSUM TABLE users, arena_events, challenges, arena_registrations EXTENDED"
                )
                rows = c.fetchall()
            conn.close()
            files = sorted(
                str(p.relative_to(uploads)) + ":" + p.read_text()
                for p in uploads.rglob("*")
                if p.is_file()
            )
            return rows, files

        before = snapshot()
        flask = str(ROOT / ".venv" / "bin" / "flask")
        env = {
            **os.environ,
            "ARENA_DEPLOY_MODE": "direct",
            "DB_HOST": host,
            "DB_PORT": port or "3306",
            "DB_USER": user,
            "DB_PASSWORD": password,
            "DB_NAME": dbname,
            "UPLOAD_DIR": str(uploads),
            "CTFD_DIR": str(ROOT),
            "FLASK": flask,
            "DATABASE_URL": url,
            "SECRET_KEY": "x" * 40,
            "FLASK_APP": "wsgi.py",
        }
        out = subprocess.run(
            [str(DEPLOY / "scripts" / "backup.sh"), str(tmp_path / "bk")],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert out.returncode == 0, out.stderr[-2000:]
        backup = pathlib.Path(out.stdout.strip().splitlines()[-1])
        assert (backup / "SHA256SUMS").exists()

        conn = pymysql.connect(
            host=host,
            port=int(port or 3306),
            user=user,
            password=password,
            database=dbname,
            autocommit=True,
        )
        with conn.cursor() as c:
            c.execute("UPDATE users SET name='tampered'")
            c.execute("DELETE FROM arena_registrations")
        conn.close()
        (uploads / "abc" / "f.txt").unlink()
        (uploads / "junk").write_text("x")
        assert snapshot() != before

        r = subprocess.run(
            [str(DEPLOY / "scripts" / "restore.sh"), str(backup)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode != 0 and "--yes" in r.stderr  # refuses without confirmation
        r = subprocess.run(
            [str(DEPLOY / "scripts" / "restore.sh"), str(backup), "--yes"],
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert r.returncode == 0, r.stderr[-3000:]
        assert snapshot() == before
        assert eid
    finally:
        destroy_ctfd(app)


def test_runner_tls_script_keeps_ca_key_out_of_platform_mount(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    work = tmp_path / "deploy"
    (work / "scripts").mkdir(parents=True)
    shutil.copy(DEPLOY / "scripts" / "runner-tls.sh", work / "scripts")
    subprocess.run(
        [str(work / "scripts" / "runner-tls.sh"), "runner.example"],
        check=True,
        capture_output=True,
    )
    mounted = work / "compose" / "runner-ca"  # mounted into platform containers
    assert sorted(p.name for p in mounted.iterdir()) == ["ca.crt"]
    assert (work / "compose" / "runner-ca-key" / "ca.key").exists()
    r = subprocess.run(
        [
            "openssl",
            "verify",
            "-CAfile",
            str(mounted / "ca.crt"),
            str(work / "compose" / "runner-tls" / "runner.crt"),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


def test_keepalive_ordering_between_nginx_and_gunicorn():
    """Regression (Phase 16 login storm): nginx must close idle upstream
    connections before gunicorn does, and must not eject a replica on one reset."""
    conf = (DEPLOY / "nginx" / "arena.conf.template").read_text()
    nginx_idle = int(re.search(r"keepalive_timeout (\d+)s;", conf).group(1))
    assert "max_fails=5" in conf
    env = _load("community.yml")["x-app"]["environment"]
    gunicorn_idle = int(
        re.search(r"--keep-alive (\d+)", env["GUNICORN_CMD_ARGS"]).group(1)
    )
    assert gunicorn_idle > nginx_idle


def test_redis_persists_sessions():
    """Regression (Phase 16 fault test): CTFd keeps login sessions in Redis; a
    non-persistent Redis logged every player out on restart."""
    conf = (DEPLOY / "redis" / "redis.conf").read_text()
    assert re.search(r"^appendonly yes$", conf, re.M)
    cache = _load("community.yml")["services"]["cache"]
    assert any(
        str(v).endswith(":/data") or ":/data " in str(v) for v in cache["volumes"]
    )


def test_cli_processes_never_run_in_debug_mode():
    """Regression (Phase 16 soak): .flaskenv put `flask arena worker` in debug
    mode, which made Flask-SQLAlchemy record every query."""
    dockerfile = (DEPLOY / "docker" / "platform.Dockerfile").read_text()
    assert "FLASK_ENV=production" in dockerfile and "FLASK_DEBUG=0" in dockerfile
    for unit in ("arena-web.service", "arena-worker.service"):
        assert "FLASK_DEBUG=0" in (DEPLOY / "systemd" / unit).read_text()
