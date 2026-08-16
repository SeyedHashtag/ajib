# External VPN API contract

ajib is an API client; the VPN panel runs on a separate server. Each configured
server supplies a base URL and an authorization token through the bot's `.env`
file. Real URLs, credentials, usernames, and server addresses must never be
committed.

## Configuration

For one server, the legacy variables are:

```dotenv
URL=https://vpn-panel.example/api-root/
TOKEN=replace-with-token
```

For multiple servers, `SERVERS_JSON` is the source of truth:

```json
[
  {
    "id": "primary",
    "name": "Primary",
    "url": "https://vpn-panel.example/api-root/",
    "token": "replace-with-token",
    "panel": "blitz",
    "enabled": true,
    "weight": 1,
    "default_inbound_ids": [],
    "default_limit_ip": 0
  },
  {
    "id": "hy2-copy",
    "name": "3x-ui Hysteria",
    "url": "https://3x-ui.example/",
    "token": "replace-with-bearer-token",
    "panel": "3x-ui",
    "enabled": false,
    "weight": 1,
    "default_inbound_ids": [],
    "default_limit_ip": 1
  }
]
```

`panel` defaults to `blitz`, so existing configurations require no migration.
Enabled 3x-ui servers require one or more `default_inbound_ids` for automatic
sales. A disabled 3x-ui server may omit them and remain available as a manual
copy destination, where the admin selects live Hysteria2 inbounds.

The CLI format is
`id=url,token[,weight,enabled[,panel[,inbound_ids[,limit_ip]]]]`; multiple
inbound IDs are separated with `|`.

The Blitz client sends the configured token in the `Authorization` request header.
The base URL may include a deployment-specific API root and is normalized with
one trailing slash.

## Blitz endpoints used by the bot

| Method | Relative path | Purpose |
| --- | --- | --- |
| `GET` | `api/v1/users/` | List users |
| `POST` | `api/v1/users/` | Create a user |
| `GET` | `api/v1/users/{username}` | Read one user |
| `PATCH` | `api/v1/users/{username}` | Edit or block a user |
| `DELETE` | `api/v1/users/{username}` | Delete a user |
| `GET` | `api/v1/users/{username}/reset` | Reset and renew a user |
| `GET` | `api/v1/users/{username}/uri` | Fetch a user's connection URI |

Successful responses should be JSON, except that the reset endpoint may return
an empty or non-JSON success response. Non-2xx responses and network failures
are treated as failed operations.

## Create-user body

```json
{
  "username": "example-user",
  "traffic_limit": 10737418240,
  "expiration_days": 30,
  "unlimited": false,
  "note": "optional metadata"
}
```

The bot relies on user records containing the fields returned by the deployed
panel, including `username`, `blocked`, traffic usage/limit values, creation
date, and expiration data where available.

## 3x-ui v3 endpoints

3x-ui uses `Authorization: Bearer <token>` and the current first-class clients
API under `/panel/api`. Legacy cookie/session-based 2.x endpoints are not
supported.

| Method | Relative path | Purpose |
| --- | --- | --- |
| `GET` | `panel/api/clients/list` | List clients with traffic and inbound membership |
| `GET` | `panel/api/clients/get/{email}` | Read the full client record |
| `POST` | `panel/api/clients/add` | Create and attach a client |
| `POST` | `panel/api/clients/update/{email}` | Replace a full client record safely |
| `POST` | `panel/api/clients/del/{email}?keepTraffic=0` | Delete a client and traffic row |
| `POST` | `panel/api/clients/resetTraffic/{email}` | Reset counters |
| `POST` | `panel/api/clients/updateTraffic/{email}` | Import upload/download counters |
| `POST` | `panel/api/clients/onlines` | Read online identities |
| `GET` | `panel/api/clients/traffic/{email}` | Read exact traffic counters |
| `GET` | `panel/api/clients/links/{email}` | Fetch direct protocol links |
| `GET` | `panel/api/inbounds/options` | Select and validate inbounds |
| `POST` | `panel/api/setting/all` | Read public subscription settings |

ajib-created 3x-ui accounts keep their configured duration in a non-secret
comment marker. This allows reset to restore delayed-start expiry. Reset fails
closed for pre-existing clients whose original duration cannot be determined.
