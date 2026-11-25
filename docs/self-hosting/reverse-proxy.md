# Reverse proxy and TLS

[Back to the documentation index](../README.md)

The shipped setup listens on `127.0.0.1:8978` only. To make the app reachable from other machines, put a reverse proxy with TLS in front of it. Do not publish port `8978` directly on a public interface.

## What the proxy has to do

- Terminate TLS and forward to `127.0.0.1:8978` (or to the container on a shared Docker network).
- Forward WebSocket upgrades. Live editing uses `/ws/...`.
- Pass the original `Host` header unchanged. The server rejects state-changing requests whose `Origin` does not match `Host`.
- Set `X-Forwarded-Proto` and `X-Forwarded-For`.
- Allow request bodies of at least 32 MB, the server's own limit.

## Tell the app to trust the proxy

By default the app ignores `X-Forwarded-*` headers. Enable them in `.env`:

```dotenv
TRUST_PROXY=true
FORWARDED_ALLOW_IPS=127.0.0.1,::1,172.16.0.0/12
```

`FORWARDED_ALLOW_IPS` lists the addresses the app accepts proxy headers from. It has to contain the address the proxy connects from, as seen inside the container:

- Proxy on the host, connecting to the published `127.0.0.1:8978`: Docker forwards the connection from the bridge gateway, usually inside `172.16.0.0/12`. Extend the default as in the example above.
- Proxy in another container on the same Compose network: add that network's subnet.
- App running without Docker on the same machine as the proxy: the default `127.0.0.1,::1` is enough.

Extend the default, do not replace it. Never use `*` with `TRUST_PROXY=true`; the server refuses to start with an open allowlist.

If proxy headers are not trusted, everything still works, but all clients share one rate-limit bucket, the `Secure` cookie flag depends on the internal scheme, and the OIDC callback URL may be derived wrongly.

## Cookies and HSTS

With `COOKIE_SECURE=auto` (default), session cookies get the `Secure` flag when the request was HTTPS, either directly or through a trusted `X-Forwarded-Proto: https`. Over HTTPS the server also sends `Strict-Transport-Security`. Set `COOKIE_SECURE=true` to force the flag regardless of headers.

## Caddy

Caddy handles TLS certificates, WebSockets and forwarding headers automatically:

```caddyfile
typst.example.com {
	reverse_proxy 127.0.0.1:8978
	request_body {
		max_size 32MB
	}
}
```

## nginx

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name typst.example.com;

    ssl_certificate     /etc/letsencrypt/live/typst.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/typst.example.com/privkey.pem;

    client_max_body_size 32m;

    location / {
        proxy_pass http://127.0.0.1:8978;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 1h;
    }
}
```

`$connection_upgrade` needs this block in the `http` context:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
```

## Checklist

- `https://typst.example.com/healthz` answers.
- Two browsers editing the same document see each other's changes live (WebSockets work).
- Signing in works and stays signed in after a reload (cookies and `Host` are correct).
- The container log shows no `X-Forwarded-Proto seen but TRUST_PROXY=false` warnings.
- If you use SSO, set `OIDC_REDIRECT_URI` to the public URL, see [Single Sign-On (OIDC)](single-sign-on.md).
