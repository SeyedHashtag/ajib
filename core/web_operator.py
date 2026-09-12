"""Optional VPS website lifecycle. No Telegram handlers or secrets at import time."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tarfile
import uuid
import tempfile
from contextlib import contextmanager
from functools import wraps

CONFIG = Path('/etc/ajib-web')
STATE = Path('/var/lib/ajib-state')
SOURCE = Path('/opt/ajib-web/app')
VENV = Path('/opt/ajib-web/venv')
EDGE_IMAGE = 'nginxinc/nginx-unprivileged:stable-alpine'
CERT_IMAGE = 'certbot/certbot:latest'
UNITS = ('ajib-web-api', 'ajib-web-worker')


def run(*args, capture=False, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, text=True,
                          stdout=subprocess.PIPE if capture else None, **kwargs)


def validate_domain(value):
    value = value.lower().strip().rstrip('.')
    if len(value) > 253 or len(value.split('.')) < 2 or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
        for label in value.split('.')
    ):
        raise ValueError('Use a DNS hostname, without a scheme, port, or path.')
    return value


def load():
    return json.loads((CONFIG / 'deployment.json').read_text())


def root_required():
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise ValueError('Website installation and service changes require root on Linux.')


def inspect_proxy():
    if not shutil.which('docker'):
        return None
    ids = run('docker', 'ps', '-q', capture=True).stdout.split()
    candidates = []
    for ident in ids:
        item = json.loads(run('docker', 'inspect', ident, capture=True).stdout)[0]
        if Path(item['Path']).name != 'traefik' and not item['Config']['Image'].startswith('traefik'):
            continue
        command = item['Config'].get('Cmd') or []
        networks = item['NetworkSettings']['Networks']
        resolvers = [v.split('.')[1] for v in command if v.startswith('--certificatesresolvers.') and '.acme.httpchallenge=true' in v]
        if '--providers.docker=true' not in command or len(networks) != 1 or len(set(resolvers)) != 1:
            raise ValueError('Existing Traefik needs a reviewed adapter: one Docker network and an HTTP-01 certificate resolver are required.')
        network, settings = next(iter(networks.items()))
        candidates.append({'container': item['Name'].lstrip('/'), 'network': network,
                           'ip': str(ipaddress.ip_address(settings['IPAddress'])),
                           'resolver': resolvers[0], 'entrypoint': 'websecure'})
    if len(candidates) > 1:
        raise ValueError('Multiple Traefik installations found; choose the intended proxy before setup.')
    return candidates[0] if candidates else None


def deployment_plan(domain, checkout, mode='auto'):
    domain = validate_domain(domain)
    checkout = Path(checkout).resolve()
    if not (checkout / 'requirements-web.txt').is_file():
        raise ValueError('The selected checkout does not contain the website backend.')
    proxy = inspect_proxy() if mode != 'standalone' else None
    if mode == 'traefik' and proxy is None:
        raise ValueError('No supported running Traefik installation was found.')
    return {'domain': domain, 'mode': 'traefik' if proxy else 'standalone', 'proxy': proxy,
            'checkout': str(checkout), 'database': str(STATE / 'ajib.db'),
            'public_portal': False, 'writes_enabled': False,
            'services': list(UNITS), 'edge': 'ajib-web-edge',
            'effects': ['Create verified backup before shared database relocation',
                        'Restart only ajib bot/hosted workers for shared database configuration',
                        'Install isolated API/worker and website proxy',
                        'Preserve existing proxy, n8n, VPN services, and their configuration']}


def write(path, text, mode=0o644, *, gid=None):
    """Atomic, durable replacement; secret temporaries are private at creation."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            if gid is not None:
                os.chown(temporary, 0, gid)
            temporary.chmod(mode)
            os.fsync(stream.fileno())
        temporary.replace(path)
        if os.name == 'posix':
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def maintenance():
    """Serialize CLI maintenance without holding a database transaction."""
    import fcntl
    CONFIG.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(CONFIG / 'maintenance.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another ajib website maintenance command is running.') from None
        yield
    finally:
        os.close(descriptor)


def maintained(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        root_required()
        with maintenance():
            if (CONFIG / 'upgrade.json').exists():
                raise ValueError('Recover the interrupted upgrade first: ajib web recover-upgrade --yes.')
            if (CONFIG / 'config-sync.json').exists():
                raise ValueError('Recover the interrupted configuration sync before other website changes: '
                                 'ajib web sync-config --recover --yes.')
            return function(*args, **kwargs)
    return wrapped


def service(role):
    command = ('-m uvicorn core.web.app:create_app --factory --uds /run/ajib-web/api.sock '
               '--proxy-headers --forwarded-allow-ips=* --no-access-log') if role == 'api' else '-m core.web.worker'
    runtime = 'RuntimeDirectory=ajib-web\nRuntimeDirectoryMode=0750\nRuntimeDirectoryPreserve=yes\n' if role == 'api' else ''
    return f'''[Unit]
Description=ajib website {role}
After=network-online.target
Wants=network-online.target
[Service]
User=ajib-web
Group=ajib-state
WorkingDirectory={SOURCE}
EnvironmentFile={CONFIG}/runtime.env
Environment=AJIB_ENV_FILE={CONFIG}/runtime.env
Environment=AJIB_BOT_ROLE={'api' if role == 'api' else 'web-worker'}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={VENV}/bin/python {command}
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
UMask=0007
{runtime}NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={STATE}
MemoryHigh=384M
MemoryMax=512M
CPUQuota=100%
[Install]
WantedBy=multi-user.target
'''


def nginx_config(plan, *, tls=False, cloudflare_ranges=()):
    proxy = plan.get('proxy')
    realip = f"set_real_ip_from {proxy['ip']};\nreal_ip_header X-Forwarded-For;\nreal_ip_recursive on;" if proxy else ''
    ranges = '\n'.join(f'{ipaddress.ip_network(item)} 1;' for item in cloudflare_ranges)
    # Traefik appends the actual peer to XFF. Only trust that exact local proxy;
    # CF-Connecting-IP is accepted only when the resulting peer is Cloudflare.
    visitor = f'''geo $remote_addr $cloudflare_peer {{ default 0; {ranges} }}
map "$cloudflare_peer:$http_cf_connecting_ip" $visitor_ip {{
 default $remote_addr;
 ~^1:([0-9a-fA-F:.]+)$ $http_cf_connecting_ip;
}}
'''
    host = plan['domain']
    locations = '''
root /site;
index index.html;
client_max_body_size 6m;
add_header X-Content-Type-Options nosniff always;
add_header Referrer-Policy no-referrer always;
add_header Content-Security-Policy "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org" always;
location /api/ {
 proxy_pass http://unix:/run/ajib-web/api.sock:;
 proxy_set_header Host $host;
 proxy_set_header X-Forwarded-Proto https;
 proxy_set_header X-Forwarded-For $visitor_ip;
 proxy_read_timeout 60s;
 access_log off;
 add_header Cache-Control "no-store" always;
}
location /assets/ { try_files $uri =404; expires 1y; }
location = /index.html { add_header Cache-Control "no-cache"; }
location ~ /\\. { deny all; }
location / { try_files $uri $uri/ /index.html; }
'''
    http = f'server {{ listen 8080; server_name {host};\n'
    if not proxy:
        http += 'location ^~ /.well-known/acme-challenge/ { root /acme; }\n'
    http += ('location / { return 301 https://$host$request_uri; }\n' if tls else locations if proxy else 'location / { return 503; }\n') + '}\n'
    if tls:
        http += f'''server {{ listen 8443 ssl; server_name {host};
ssl_certificate /certificates/live/{host}/fullchain.pem;
ssl_certificate_key /certificates/live/{host}/privkey.pem;
{locations}
}}
'''
    return f'''pid /tmp/nginx.pid;
events {{ worker_connections 256; }}
http {{
include /etc/nginx/mime.types;
default_type application/octet-stream;
access_log off;
error_log /dev/stderr warn;
client_body_temp_path /tmp/client_temp;
proxy_temp_path /tmp/proxy_temp;
fastcgi_temp_path /tmp/fastcgi_temp;
uwsgi_temp_path /tmp/uwsgi_temp;
scgi_temp_path /tmp/scgi_temp;
{realip}
{visitor}
{http}
}}
'''


def edge_args(plan, uid, gid):
    args = ['docker', 'run', '-d', '--name', 'ajib-web-edge', '--restart', 'unless-stopped',
            '--read-only', '--memory', '128m', '--cpus', '0.5', '--pids-limit', '64',
            '--security-opt', 'no-new-privileges', '--cap-drop', 'ALL',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=32m', '--user', f'{uid}:{gid}',
            '-v', f'{SOURCE}/web/dist:/site:ro', '-v', f'{CONFIG}/edge:/etc/ajib-edge:ro',
            '-v', '/run/ajib-web:/run/ajib-web:ro', '-v', f'{CONFIG}/acme:/acme:ro']
    proxy = plan.get('proxy')
    if proxy:
        args += ['--network', proxy['network'], '--label', 'traefik.enable=true',
                 '--label', f"traefik.docker.network={proxy['network']}",
                 '--label', f"traefik.http.routers.ajib-web.rule=Host(`{plan['domain']}`)",
                 '--label', f"traefik.http.routers.ajib-web.entrypoints={proxy['entrypoint']}",
                 '--label', 'traefik.http.routers.ajib-web.tls=true',
                 '--label', f"traefik.http.routers.ajib-web.tls.certresolver={proxy['resolver']}",
                 '--label', 'traefik.http.services.ajib-web.loadbalancer.server.port=8080']
    else:
        args += ['-p', '80:8080', '-p', '443:8443', '-v', f'{CONFIG}/certificates:/certificates:ro']
    return args + [EDGE_IMAGE, 'nginx', '-c', '/etc/ajib-edge/nginx.conf', '-g', 'daemon off;']


def snapshot_database(source, target):
    source, target = Path(source), Path(target)
    if not source.is_file() or target.exists():
        raise ValueError('Backup requires an existing source and a new destination.')
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as origin:
        with sqlite3.connect(target) as destination:
            origin.backup(destination)
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('SQLite backup integrity check failed.')
    target.chmod(0o600)


@maintained
def setup(plan, *, email=None):
    """Explicit CLI installation. Financial writes always remain disabled."""
    root_required()
    # This is a root-only, short-lived installer process. Private artifacts have
    # explicit modes below; public code must remain readable with a strict caller umask.
    os.umask(0o022)
    import grp
    import pwd
    import requests
    from dotenv import dotenv_values
    checkout = Path(plan['checkout'])
    bot = checkout / 'core/scripts/telegrambot'
    if not (bot / 'utils/web_auth.py').exists():
        raise ValueError('Install compatible bot authentication adapters before website setup.')
    memory = Path('/proc/meminfo').read_text()
    available = int(re.search(r'MemAvailable:\s+(\d+)', memory)[1])
    if available < 768 * 1024:
        raise ValueError('Less than 768 MB available RAM; increase capacity before installing the backend.')
    if (CONFIG / 'deployment.json').exists():
        previous = load()
        if (previous['domain'], previous['mode']) != (plan['domain'], plan['mode']):
            raise ValueError('Changing the installed domain/proxy requires a reviewed migration.')
    if not shutil.which('docker'):
        run('apt-get', 'update', '-qq')
        run('apt-get', 'install', '-y', 'docker.io')
    if plan['mode'] == 'standalone':
        import socket
        for port in (80, 443):
            with socket.socket() as probe:
                try:
                    probe.bind(('0.0.0.0', port))
                except OSError as exc:
                    existing = subprocess.run(['docker', 'inspect', 'ajib-web-edge'], capture_output=True)
                    if existing.returncode:
                        raise ValueError(f'Port {port} is occupied; existing services were not changed.') from exc
    try:
        gid = grp.getgrnam('ajib-state').gr_gid
    except KeyError:
        run('groupadd', '--system', 'ajib-state')
        gid = grp.getgrnam('ajib-state').gr_gid
    try:
        uid = pwd.getpwnam('ajib-web').pw_uid
    except KeyError:
        run('useradd', '--system', '--gid', 'ajib-state', '--no-create-home', '--shell', '/usr/sbin/nologin', 'ajib-web')
        uid = pwd.getpwnam('ajib-web').pw_uid
    for directory in (CONFIG, STATE):
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, 0, gid)
        directory.chmod(0o2750 if directory == CONFIG else 0o2770)
    (CONFIG / 'acme').mkdir(exist_ok=True)
    (CONFIG / 'certificates').mkdir(exist_ok=True)
    (CONFIG / 'edge').mkdir(exist_ok=True)
    (CONFIG / 'edge').chmod(0o755)
    secret = dict(dotenv_values(bot / '.env'))
    token = secret.get('API_TOKEN', '')
    # Never include token-bearing URLs in exceptions or logs.
    try:
        identity = requests.get('https://api.telegram.org/bot' + token + '/getMe', timeout=20).json()
        if not identity.get('ok'):
            raise ValueError('Telegram rejected the configured bot identity.')
        username = identity['result']['username']
    except requests.RequestException:
        raise ValueError('Telegram identity check failed; retry setup.') from None
    if not (checkout / 'web/dist/index.html').exists():
        if shutil.which('npm'):
            run('npm', 'ci', cwd=checkout / 'web')
            run('npm', 'run', 'build', cwd=checkout / 'web')
        else:
            run('docker', 'run', '--rm', '--memory', '1g', '-v', f'{checkout}/web:/app', '-w', '/app',
                'node:22-bookworm-slim', 'sh', '-c', 'npm ci && npm run build')
    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    SOURCE.parent.chmod(0o755)
    # Reconfiguration never modifies modules beneath a running web interpreter.
    for unit in UNITS:
        if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0:
            run('systemctl', 'stop', unit)
    # Only copy code; production state and secrets never enter the web source tree.
    for relative in ('core', 'requirements.txt', 'requirements-web.txt', 'web/dist'):
        src, dest = checkout / relative, SOURCE / relative
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('.env*', '*.db*', '*.json', '*.log', '__pycache__', 'logs', 'hosted_bots', 'broadcast_logs'))
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    for name in ('plans.json', 'support_info.json'):
        if (bot / name).is_file():
            shutil.copyfile(bot / name, SOURCE / 'core/scripts/telegrambot' / name)
    if (checkout / 'core/web/release-contract.json').is_file():
        shutil.copyfile(checkout / 'core/web/release-contract.json', SOURCE / 'core/web/release-contract.json')
    for path in SOURCE.rglob('*'):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)
    SOURCE.chmod(0o755)
    run(sys.executable, '-m', 'venv', VENV)
    run(VENV / 'bin/pip', 'install', '--disable-pip-version-check', '-r', SOURCE / 'requirements-web.txt')
    for role in ('api', 'worker'):
        write(Path('/etc/systemd/system') / f'ajib-web-{role}.service', service(role))
    secret.update({'AJIB_DB_PATH': str(STATE / 'ajib.db'), 'AJIB_DB_SHARED_GROUP': 'ajib-state',
                   'AJIB_SQLITE_ACTIVE': '1', 'AJIB_BOT_DIR': str(SOURCE / 'core/scripts/telegrambot'),
                   'AJIB_WEB_ORIGIN': 'https://' + plan['domain'], 'AJIB_WEB_BOT_USERNAME': username,
                   'AJIB_WEB_PUBLIC_PORTAL': '0', 'AJIB_WEB_WRITES_ENABLED': '0', 'AJIB_WEB_PILOT_USERS': ''})
    env_text = '\n'.join(k + '=' + json.dumps(str(v), ensure_ascii=False) for k, v in secret.items() if v is not None) + '\n'
    write(CONFIG / 'runtime.env', env_text, 0o640)
    os.chown(CONFIG / 'runtime.env', 0, gid)
    active = subprocess.run(['systemctl', 'is-active', '--quiet', 'ajib-telegram-bot']).returncode == 0
    old_database = Path(os.environ.get('AJIB_DB_PATH', str(bot / 'ajib.db')))
    if not (STATE / 'ajib.db').exists():
        if active:
            run('systemctl', 'stop', 'ajib-telegram-bot')
        try:
            stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
            backup = Path('/opt/ajib-backups') / ('web-install-' + stamp)
            backup.mkdir(parents=True, mode=0o700)
            snapshot_database(old_database, backup / 'ajib.db')
            shutil.copy2(bot / '.env', backup / 'bot.env')
            (backup / 'bot.env').chmod(0o600)
            snapshot_database(backup / 'ajib.db', STATE / 'ajib.db')
            os.chown(STATE / 'ajib.db', 0, gid)
            (STATE / 'ajib.db').chmod(0o660)
            write('/etc/systemd/system/ajib-telegram-bot.service.d/web-state.conf',
                  f'[Service]\nEnvironment=AJIB_DB_PATH={STATE}/ajib.db\nEnvironment=AJIB_DB_SHARED_GROUP=ajib-state\nUMask=0007\n')
            run('systemctl', 'daemon-reload')
        finally:
            if active:
                run('systemctl', 'start', 'ajib-telegram-bot')
    ranges = []
    for family in ('v4', 'v6'):
        response = requests.get(f'https://www.cloudflare.com/ips-{family}', timeout=20)
        response.raise_for_status()
        ranges.extend(str(ipaddress.ip_network(item)) for item in response.text.split())
    plan.update({'bot_username': username, 'cloudflare_ranges': ranges, 'uid': uid, 'gid': gid})
    write(CONFIG / 'deployment.json', json.dumps(plan, indent=2) + '\n', 0o640)
    os.chown(CONFIG / 'deployment.json', 0, gid)
    write(CONFIG / 'edge/nginx.conf', nginx_config(plan, cloudflare_ranges=ranges))
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', *UNITS)
    run('systemctl', 'restart', *UNITS)
    # Refuse to publish unless the private API socket is serving successfully.
    for attempt in range(30):
        check = subprocess.run(['curl', '-fsS', '--unix-socket', '/run/ajib-web/api.sock',
                                'http://localhost/api/v1/health'], capture_output=True)
        if check.returncode == 0:
            break
        time.sleep(1)
    else:
        raise ValueError('API health check failed; website routing was not changed. Check ajib web logs.')
    run('docker', 'pull', EDGE_IMAGE)
    # Check configuration in a disposable container before replacing only our edge.
    args = edge_args(plan, uid, gid)
    check_args = ['docker', 'run', '--rm', '--user', f'{uid}:{gid}', '-v', f'{CONFIG}/edge:/etc/ajib-edge:ro',
                  EDGE_IMAGE, 'nginx', '-c', '/etc/ajib-edge/nginx.conf', '-t']
    run(*check_args)
    existing = subprocess.run(['docker', 'inspect', 'ajib-web-edge'], capture_output=True)
    if existing.returncode == 0:
        run('docker', 'rm', '-f', 'ajib-web-edge')
    run(*args)
    if plan['mode'] == 'standalone':
        cert_args = ['docker', 'run', '--rm', '-v', f'{CONFIG}/certificates:/etc/letsencrypt',
                     '-v', f'{CONFIG}/acme:/acme', CERT_IMAGE, 'certonly', '--webroot', '-w', '/acme',
                     '-d', plan['domain'], '--agree-tos', '--non-interactive']
        cert_args += ['--email', email] if email else ['--register-unsafely-without-email']
        run(*cert_args)
        # Dedicated certificate copy readable only by the website service group.
        for path in (CONFIG / 'certificates').rglob('*'):
            if path.is_symlink():
                continue
            os.chown(path, 0, gid)
            path.chmod(0o750 if path.is_dir() else 0o640)
        write(CONFIG / 'edge/nginx.conf', nginx_config(plan, tls=True, cloudflare_ranges=ranges))
        run('docker', 'exec', 'ajib-web-edge', 'nginx', '-c', '/etc/ajib-edge/nginx.conf', '-s', 'reload')
        write('/etc/systemd/system/ajib-web-certificate.service',
              f'[Unit]\nDescription=Renew ajib Let\'s Encrypt certificate\n[Service]\nType=oneshot\nExecStart={checkout}/ajib_venv/bin/python {checkout}/core/cli.py web renew-certificate\n')
        write('/etc/systemd/system/ajib-web-certificate.timer',
              '[Unit]\nDescription=Check ajib TLS renewal\n[Timer]\nOnCalendar=*-*-* 03,15:00:00\nRandomizedDelaySec=3600\nPersistent=true\n[Install]\nWantedBy=timers.target\n')
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', '--now', 'ajib-web-certificate.timer')
    return {'url': 'https://' + plan['domain'], 'access': 'Public information; administrators only; financial writes disabled.'}


def status():
    config = load()
    from dotenv import dotenv_values
    environment = dotenv_values(CONFIG / 'runtime.env')
    return {'url': 'https://' + config['domain'], 'mode': config['mode'],
            'services': {name: subprocess.run(['systemctl', 'is-active', name], capture_output=True, text=True).stdout.strip() for name in UNITS},
            'database': config['database'], 'writes_enabled': environment.get('AJIB_WEB_WRITES_ENABLED') == '1',
            'public_portal': environment.get('AJIB_WEB_PUBLIC_PORTAL') == '1',
            'configuration_recovery_pending': (CONFIG / 'config-sync.json').exists()}


@maintained
def renew_certificate():
    root_required()
    plan = load()
    if plan['mode'] != 'standalone':
        return 'Traefik manages automatic Let\'s Encrypt renewal.'
    run('docker', 'run', '--rm', '-v', f'{CONFIG}/certificates:/etc/letsencrypt', '-v', f'{CONFIG}/acme:/acme',
        CERT_IMAGE, 'renew', '--quiet')
    for path in (CONFIG / 'certificates').rglob('*'):
        if not path.is_symlink():
            os.chown(path, 0, plan['gid'])
            path.chmod(0o750 if path.is_dir() else 0o640)
    run('docker', 'exec', 'ajib-web-edge', 'nginx', '-c', '/etc/ajib-edge/nginx.conf', '-t')
    run('docker', 'exec', 'ajib-web-edge', 'nginx', '-c', '/etc/ajib-edge/nginx.conf', '-s', 'reload')
    return 'Certificate renewal checked.'


def backup_configuration():
    """Companion to the SQLite ZIP; secrets are private from file creation onward."""
    if not (CONFIG / 'deployment.json').is_file():
        return None
    destination = Path(os.getenv('AJIB_BACKUP_DIR', '/opt/ajib-backups'))
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f'ajib_web_config_{time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}_{uuid.uuid4().hex[:8]}.tar.gz'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream, tarfile.open(fileobj=stream, mode='w:gz') as archive:
        archive.add(CONFIG, arcname='etc/ajib-web')
        for unit in (*UNITS, 'ajib-web-certificate'):
            source = Path('/etc/systemd/system') / (unit + '.service')
            if source.is_file():
                archive.add(source, arcname='etc/systemd/system/' + source.name)
        dropin = Path('/etc/systemd/system/ajib-telegram-bot.service.d/web-state.conf')
        if dropin.is_file():
            archive.add(dropin, arcname='etc/systemd/system/ajib-telegram-bot.service.d/web-state.conf')
    return str(path)
