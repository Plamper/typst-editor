# Single Sign-On (OIDC)

[Back to the documentation index](../README.md)

Typst Editor can sign users in through any OpenID Connect provider, for example Authentik, Authelia or Kanidm. It uses the Authorization Code Flow with PKCE and works with both confidential clients (with a client secret) and public clients (PKCE only).

## 1. Register a client at your provider

Create an OAuth2 / OpenID Connect application with:

- Grant type: Authorization Code.
- Redirect URI: `https://typst.example.com/api/oidc/callback` (your public URL plus `/api/oidc/callback`).
- Scopes: `openid profile email`, plus `groups` if you restrict access by group.
- Client type: confidential (note the secret) or public.

Note the issuer URL. The app loads `<issuer>/.well-known/openid-configuration` to discover the endpoints.

## 2. Configure the app

Add to `.env` (or `config.toml`):

```dotenv
OIDC_ISSUER_URL=https://auth.example.com/application/o/typst/
OIDC_CLIENT_ID=typst-editor
OIDC_CLIENT_SECRET=
OIDC_REDIRECT_URI=https://typst.example.com/api/oidc/callback
OIDC_PROVIDER_NAME=Authentik
```

OIDC is enabled as soon as both `OIDC_ISSUER_URL` and `OIDC_CLIENT_ID` are set. Leave `OIDC_CLIENT_SECRET` empty for a public PKCE client.

Restart the container (`docker compose up -d`). The login page now shows a sign-in button labelled with `OIDC_PROVIDER_NAME`.

## Settings

| Setting | Default | Description |
|---|---|---|
| `OIDC_ISSUER_URL` | empty | Issuer URL of your provider. Empty disables OIDC. |
| `OIDC_CLIENT_ID` | empty | Client ID. Required together with the issuer URL. |
| `OIDC_CLIENT_SECRET` | empty | Client secret. Empty for public PKCE clients. |
| `OIDC_REDIRECT_URI` | empty | Explicit callback URL. If empty, it is derived from the request. Set it in production. |
| `OIDC_PROVIDER_NAME` | `SSO` | Label of the sign-in button. |
| `OIDC_SCOPES` | `openid profile email` | Space-separated scopes. `groups` is added automatically when `OIDC_ALLOWED_GROUPS` is set. |
| `OIDC_USERNAME_CLAIM` | `preferred_username` | Claim used to choose the username for new accounts. |
| `OIDC_ALLOWED_GROUPS` | empty | Comma-separated groups allowed to sign in. Empty allows every user of the provider. |
| `OIDC_AUTO_REGISTER` | `true` | Create an account on the first OIDC login. Independent of `REGISTRATION`. |
| `OIDC_AUTO_REDIRECT` | `false` | Send signed-out visitors straight to the provider. |
| `DISABLE_PASSWORD_LOGIN` | `false` | Turn off local username and password login and registration. Requires OIDC. |

## How accounts are matched

- An OIDC account is identified by the issuer and the `sub` claim, not by its name or email. Renaming a user at the provider does not create a new account.
- On the first login, the username is taken from `OIDC_USERNAME_CLAIM`, falling back to `preferred_username`, `nickname`, `email` (only the part before `@`, and only if not marked unverified) and `name`. It is cleaned up to letters, digits, `_` and `-`, at most 20 characters. If the name is taken, a suffix such as `-2` is added.
- Existing local accounts are not linked to OIDC identities automatically.
- With `OIDC_AUTO_REGISTER=false`, only identities that already have an account can sign in.

## Restricting access by group

```dotenv
OIDC_ALLOWED_GROUPS=typst-users,typst-admins
```

The app reads the `groups` claim from the ID token, or from the userinfo endpoint if the token does not contain it. Users outside the listed groups get an "access denied" error. Make sure your provider actually releases a `groups` claim for this client.

## SSO only

To make the provider the only way in:

```dotenv
DISABLE_PASSWORD_LOGIN=true
OIDC_AUTO_REDIRECT=true
```

With `DISABLE_PASSWORD_LOGIN=true` the password form and local registration disappear. The server refuses to start if OIDC is not fully configured, so you cannot lock yourself out by accident.

## Invite links

Project invite links also work with SSO: a signed-out user who opens an invite link is sent through the provider and joins the project after signing in.

## Troubleshooting

After a failed login the app redirects to `/?oidc_error=<code>` and logs the details:

| Code | Meaning |
|---|---|
| `access_denied` | The provider denied the request, or the user is not in `OIDC_ALLOWED_GROUPS`. |
| `registration_disabled` | No account exists for this identity and `OIDC_AUTO_REGISTER=false`. |
| `state_invalid` | The login took too long (over 10 minutes), was opened twice, or the callback URL does not match. |
| `rate_limited` | Too many login attempts, see `RATE_LOGIN_PER_MIN` and `RATE_AUTH_PER_MIN`. |
| `failed` | Anything else: discovery, token exchange or token validation failed. Check the container log. |

Common causes:

- Redirect URI mismatch: the value at the provider must equal `OIDC_REDIRECT_URI` exactly.
- Wrong scheme or host in the derived callback URL: set `OIDC_REDIRECT_URI`, or configure proxy trust as described in [Reverse proxy and TLS](reverse-proxy.md).
- The container cannot reach the issuer URL: test with `docker compose exec app python -c "import urllib.request;print(urllib.request.urlopen('https://auth.example.com/application/o/typst/.well-known/openid-configuration').status)"`.
