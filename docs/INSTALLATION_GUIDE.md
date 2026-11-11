# Installation guide

There are two ways to install:

- **Docker Compose** (recommended): `deploy/README.md` covers Profiles A, B
  and C. All three were brought up and verified in Phase 15.
- **Bare metal** (below): Debian/Ubuntu with system packages and systemd.

Only free, open-source software is used.

## Requirements

| Component | Version tested |
|---|---|
| Python | 3.11 |
| MariaDB | 10.11 (MySQL 8 should work, but was not tested) |
| Redis | 7.x |
| nginx | 1.28 (the `resolve` upstream parameter needs ≥ 1.27.3; drop it on older nginx) |
| Docker Engine + Compose v2 | 29.x / v5.x (runner host and compose profiles only) |

## Bare-metal install (platform host)

```bash
sudo apt-get install -y python3.11 python3.11-venv mariadb-server redis-server nginx git
```

```bash
sudo useradd --system --home /opt/arena --shell /usr/sbin/nologin ctfd
```

```bash
sudo git clone <your-arena-repository-url> /opt/arena
```

```bash
sudo python3.11 -m venv /opt/arena/.venv
```

```bash
sudo /opt/arena/.venv/bin/pip install -r /opt/arena/requirements.txt
```

```bash
sudo mkdir -p /var/lib/arena/uploads /var/log/arena /etc/arena
```

```bash
sudo chown -R ctfd:ctfd /var/lib/arena /var/log/arena /opt/arena
```

### Database

MariaDB must use utf8mb4 with `utf8mb4_unicode_ci`. Copy
`deploy/mariadb/arena.cnf` to `/etc/mysql/mariadb.conf.d/60-arena.cnf` and
restart MariaDB. Then create the database and user (replace the password):

```bash
sudo mariadb -e "CREATE DATABASE ctfd CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE USER 'ctfd'@'localhost' IDENTIFIED BY 'CHANGE_ME_DB_PASSWORD'; GRANT ALL ON ctfd.* TO 'ctfd'@'localhost';"
```

### Redis

CTFd keeps login sessions in Redis, so turn on persistence (AOF) and set a
password. Use the settings in `deploy/redis/redis.conf` (`appendonly yes`,
`maxmemory`) together with `requirepass` in `/etc/redis/redis.conf`.

### Environment file `/etc/arena/arena.env` (mode 600, owner root)

```
SECRET_KEY=<48 random chars>
ARENA_SECRET_KEY=<another 48 random chars>
DATABASE_URL=mysql+pymysql://ctfd:CHANGE_ME_DB_PASSWORD@localhost/ctfd
REDIS_URL=redis://:CHANGE_ME_REDIS_PASSWORD@localhost:6379/0
UPLOAD_FOLDER=/var/lib/arena/uploads
LOG_FOLDER=/var/log/arena
REVERSE_PROXY=true
WORKERS=2
ARENA_RUNNER_MODE=mock
ARENA_CERTIFICATES_ENABLED=false
```

`deploy/scripts/gen-secrets.sh <file>` fills every `CHANGE_ME` in a copy of
`deploy/examples/platform.env.example`.

### Migrate and start

```bash
cd /opt/arena && sudo -u ctfd env $(sudo cat /etc/arena/arena.env | xargs) .venv/bin/flask db upgrade
```

```bash
cd /opt/arena && sudo -u ctfd env $(sudo cat /etc/arena/arena.env | xargs) .venv/bin/flask arena migrate
```

```bash
sudo cp /opt/arena/deploy/systemd/arena-web.service /opt/arena/deploy/systemd/arena-worker.service /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now arena-web arena-worker
```

### nginx

Render the template, putting your domain in `ARENA_DOMAIN`. Change the
upstream to `server 127.0.0.1:8000 max_fails=5 fail_timeout=5s;`, then
remove the `resolver` line and the `zone`/`resolve` keywords:

```bash
ARENA_DOMAIN=ctf.example.org ARENA_RATELIMIT_EXEMPT_CIDR=192.0.2.255/32 envsubst '${ARENA_DOMAIN} ${ARENA_RATELIMIT_EXEMPT_CIDR}' < /opt/arena/deploy/nginx/arena.conf.template | sudo tee /etc/nginx/conf.d/arena.conf
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Open `https://<domain>/setup` and create the admin account.

### Verify

```bash
curl -s https://<domain>/arena/readyz
```

```bash
cd /opt/arena && sudo -u ctfd env $(sudo cat /etc/arena/arena.env | xargs) .venv/bin/flask arena check-integrity
```

```bash
cd /opt/arena && sudo -u ctfd env $(sudo cat /etc/arena/arena.env | xargs) .venv/bin/flask arena worker-health
```

The backup and restore scripts work on bare metal with
`ARENA_DEPLOY_MODE=direct` (see `deploy/scripts/lib.sh`). That mode is
exercised by `tests/arena/test_deploy.py::test_backup_restore_round_trip_direct_mode`.

## Runner host

Always use Docker (Profile C). See `deploy/README.md` § Profile C and
RUNNER_SECURITY.md.
