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
    "enabled": true,
    "weight": 1
  }
]
```

The client sends the configured token in the `Authorization` request header.
The base URL may include a deployment-specific API root and is normalized with
one trailing slash.

## Endpoints used by the bot

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
