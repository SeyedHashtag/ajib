import json
from pathlib import Path
import sqlite3
import sys
import os
import tempfile
import subprocess

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
import web_operator as web
from web_cli import web_group, apply_database_environment


@pytest.mark.parametrize('domain', ['https://example.com', 'host:443', 'a..com', '-a.com', 'a/b.com', 'a.com\nserver', '*.a.com'])
def test_domain_cannot_inject_proxy_configuration(domain):
    with pytest.raises(ValueError):
        web.validate_domain(domain)


def plan(proxy=True):
    return {'domain': 'utility.example.com', 'proxy': {'ip': '172.18.0.3', 'network': 'existing',
            'entrypoint': 'websecure', 'resolver': 'existing-le'} if proxy else None}


def test_existing_proxy_adds_only_host_route_and_no_published_ports():
    args = web.edge_args(plan(), 998, 997)
    assert '-p' not in args
    assert '--network' in args
    assert 'traefik.http.routers.ajib-web.rule=Host(`utility.example.com`)' in args
    assert 'traefik.http.routers.ajib-web.tls.certresolver=existing-le' in args
    assert '127.0.0.1:8080' not in ' '.join(args)
    assert not any('docker.sock' in arg for arg in args)
    assert '/run/ajib-web:/run/ajib-web:ro' in args


def test_cloudflare_header_is_trusted_only_after_proxy_peer_validation():
    config = web.nginx_config(plan(), cloudflare_ranges=['173.245.48.0/20'])
    assert 'set_real_ip_from 172.18.0.3;' in config
    assert '0.0.0.0/0' not in config
    assert 'geo $remote_addr $cloudflare_peer' in config
    assert 'default $remote_addr;' in config
    assert 'Cache-Control "no-store"' in config
    assert 'proxy_pass http://unix:/run/ajib-web/api.sock:;' in config


def test_standalone_serves_only_acme_until_certificate_exists():
    config = web.nginx_config(plan(False))
    assert 'location / { return 503; }' in config
    assert '/.well-known/acme-challenge/' in config
    assert 'location /api/' not in config
    tls = web.nginx_config(plan(False), tls=True)
    assert 'listen 8443 ssl' in tls
    assert 'return 301 https://' in tls


def test_snapshot_includes_committed_wal_and_preserves_source(tmp_path):
    source, target = tmp_path / 'source.db', tmp_path / 'snapshot.db'
    writer = sqlite3.connect(source)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('CREATE TABLE payments (id INTEGER)')
    writer.execute('INSERT INTO payments VALUES (7)')
    writer.commit()
    web.snapshot_database(source, target)
    with sqlite3.connect(target) as check:
        assert check.execute('SELECT id FROM payments').fetchone()[0] == 7
    assert writer.execute('SELECT id FROM payments').fetchone()[0] == 7
    with pytest.raises(ValueError):
        web.snapshot_database(source, target)
    writer.close()


def test_setup_dry_run_does_not_apply(monkeypatch):
    monkeypatch.setattr(web, 'deployment_plan', lambda *args: {'domain': args[0]})
    monkeypatch.setattr(web, 'setup', lambda *args, **kwargs: pytest.fail('must not mutate'))
    runner = CliRunner()
    result = runner.invoke(web_group, ['setup', '--domain', 'example.com', '--dry-run'])
    assert result.exit_code == 0
    result = runner.invoke(web_group, ['setup', '--domain', 'example.com'])
    assert result.exit_code != 0
    assert '--yes' in result.output


def test_operator_database_path_cannot_silently_diverge(monkeypatch, tmp_path):
    monkeypatch.setattr(web, 'CONFIG', tmp_path)
    database = str(tmp_path / 'shared.db')
    (tmp_path / 'deployment.json').write_text(json.dumps({'database': database}))
    monkeypatch.setenv('AJIB_DB_PATH', '')
    monkeypatch.setenv('AJIB_DB_SHARED_GROUP', '')
    apply_database_environment()
    assert __import__('os').environ['AJIB_DB_PATH'] == database
    monkeypatch.setenv('AJIB_DB_PATH', str(tmp_path / 'wrong.db'))
    with pytest.raises(Exception, match='conflicts'):
        apply_database_environment()


def test_web_services_have_no_tcp_listener_or_root_identity():
    for role in ('api', 'worker'):
        config = web.service(role)
        assert 'User=ajib-web' in config
        assert 'MemoryMax=512M' in config
        assert '--host 0.0.0.0' not in config
    assert '--uds /run/ajib-web/api.sock' in web.service('api')
    assert 'RuntimeDirectoryPreserve=yes' in web.service('api')


def test_configuration_backup_keeps_secret_file_private(monkeypatch, tmp_path):
    import tarfile
    config = tmp_path / 'config'
    config.mkdir()
    (config / 'deployment.json').write_text('{}')
    (config / 'runtime.env').write_text('API_TOKEN=synthetic-only')
    monkeypatch.setattr(web, 'CONFIG', config)
    monkeypatch.setenv('AJIB_BACKUP_DIR', str(tmp_path / 'backups'))
    path = Path(web.backup_configuration())
    if os.name == 'posix':
        assert path.stat().st_mode & 0o777 == 0o600
    with tarfile.open(path) as archive:
        assert archive.extractfile('etc/ajib-web/runtime.env').read() == b'API_TOKEN=synthetic-only'


@pytest.mark.skipif(not hasattr(os, 'geteuid') or (hasattr(os, 'geteuid') and os.geteuid() != 0), reason='Requires two Linux service identities')
def test_shared_database_repairs_root_created_shm_for_web_identity(monkeypatch):
    import grp
    import pwd
    from utils import database
    nobody = pwd.getpwnam('nobody')
    group = grp.getgrgid(nobody.pw_gid)
    with tempfile.TemporaryDirectory(prefix='ajib-two-identities-') as root:
        path = Path(root) / 'shared.db'
        monkeypatch.setenv('AJIB_DB_PATH', str(path))
        monkeypatch.setenv('AJIB_DB_SHARED_GROUP', group.gr_name)
        monkeypatch.setenv('AJIB_BOT_DIR', root + '/source')
        writer = sqlite3.connect(path)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE probe (value INTEGER)')
        writer.commit()
        Path(str(path) + '-shm').chmod(0o644)
        database._ensure_permissions(str(path))
        def service_identity():
            os.setgroups([])
            os.setgid(nobody.pw_gid)
            os.setuid(nobody.pw_uid)
        result = subprocess.run(['/usr/bin/python3', '-c',
            'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("INSERT INTO probe VALUES (1)"); c.commit()', str(path)],
            preexec_fn=service_identity, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert writer.execute('SELECT count(*) FROM probe').fetchone()[0] == 1
        writer.close()
