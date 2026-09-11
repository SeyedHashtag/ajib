"""Render reviewable VPS configs; this command never installs or starts services."""
import argparse
from pathlib import Path
import re


def linux_path(value):
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', value) or '..' in value.split('/'):
        raise argparse.ArgumentTypeError('Use an absolute Linux path without spaces or parent traversal')
    return value.rstrip('/')


def domain(value):
    if len(value) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', value) or '.' not in value or '..' in value:
        raise argparse.ArgumentTypeError('Use a lowercase DNS hostname')
    return value


def render(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    for role, command in (
        ('api', '-m uvicorn core.web.app:create_app --factory --host 127.0.0.1 --port 8080 --proxy-headers --forwarded-allow-ips=127.0.0.1 --no-access-log'),
        ('worker', '-m core.web.worker'),
    ):
        content = f'''[Unit]
Description=ajib web {role}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ajib-web
Group=ajib-state
WorkingDirectory={args.checkout}
EnvironmentFile={args.env_file}
Environment=AJIB_ENV_FILE={args.env_file}
Environment=AJIB_BOT_ROLE={'api' if role == 'api' else 'web-worker'}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={args.python} {command}
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
UMask=0007
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
ReadWritePaths={args.state_directory}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
'''
        (output / f'ajib-web-{role}.service').write_text(content, encoding='utf-8', newline='\n')
    nginx = f'''# Include from nginx's http context. Review certificate paths before installation.
limit_req_zone $binary_remote_addr zone=ajib_login:10m rate=30r/m;
server {{
    listen 80;
    server_name {args.domain};
    location /.well-known/acme-challenge/ {{ root /var/www/acme; }}
    location / {{ return 301 https://$host$request_uri; }}
}}
server {{
    listen 443 ssl;
    server_name {args.domain};
    ssl_certificate /etc/letsencrypt/live/{args.domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{args.domain}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    root {args.checkout}/web/dist;
    index index.html;
    client_max_body_size 6m;
    server_tokens off;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org" always;
    location ~ /\\. {{ deny all; }}
    location ^~ /api/v1/auth/ {{
        limit_req zone=ajib_login burst=30 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 60s;
        access_log off;
    }}
    location /api/ {{
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 60s;
        access_log off;
    }}
    location /assets/ {{ try_files $uri =404; expires 1y; }}
    location = /index.html {{ expires -1; }}
    location / {{ try_files $uri $uri/ /index.html; }}
}}
'''
    (output / 'ajib-web.nginx.conf').write_text(nginx, encoding='utf-8', newline='\n')
    environment = f'''# Merge the required existing bot/panel/payment settings through the CLI.
# Store this outside the repository, owned by root:ajib-state with mode 0640.
AJIB_SQLITE_ACTIVE=1
AJIB_BOT_DIR={args.checkout}/core/scripts/telegrambot
AJIB_DB_PATH={args.state_directory}/ajib.db
AJIB_DB_SHARED_GROUP=ajib-state
AJIB_WEB_ORIGIN=https://{args.domain}
AJIB_WEB_BOT_USERNAME=
AJIB_WEB_PUBLIC_PORTAL=0
AJIB_WEB_WRITES_ENABLED=0
AJIB_WEB_PILOT_USERS=
'''
    (output / 'web.env.example').write_text(environment, encoding='utf-8', newline='\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain', required=True, type=domain)
    parser.add_argument('--checkout', required=True, type=linux_path)
    parser.add_argument('--python', required=True, type=linux_path)
    parser.add_argument('--state-directory', required=True, type=linux_path)
    parser.add_argument('--env-file', required=True, type=linux_path)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.state_directory == args.checkout or args.state_directory.startswith(args.checkout + '/'):
        parser.error('Use a dedicated state directory outside the source checkout')
    print(f'Rendered configuration for review in {render(args)}; no services changed.')


if __name__ == '__main__':
    main()
