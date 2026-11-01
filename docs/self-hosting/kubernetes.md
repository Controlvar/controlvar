# Kubernetes (Helm)

Controlvar installs on Kubernetes with the Helm chart in `charts/experimently`: a
one-time secret step, then one install command.

The four blocks on this page are tested as written on kind with ingress-nginx: the
`chart-kind` CI job extracts them from this page and runs them in order. They need
`kubectl`, `helm` 3.14 or later, `openssl`, `curl` and `jq`, and read four shell
variables that you set first:

| Variable | Value |
|---|---|
| `CHART` | the chart: a path to `charts/experimently`, or a packaged chart |
| `PUBLIC_BASE_URL` | the origin people open, such as `https://controlvar.example.com`: a DNS name, no port, no path |
| `ADMIN_EMAIL` | the email of the first administrator |
| `PROFILE` | `core` or `full` |

## Secrets

Run this once. It creates the namespace and a Secret with generated values, and never
replaces one: run again, it fails with `AlreadyExists` and leaves the Secret as it was.
The `full` profile signs its audit log with `AUDIT_HMAC_KEY`; the `core` profile does
not read it.

<!-- chart-kind: secrets -->
```bash
kubectl create namespace experimently --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic experimently-secrets --namespace experimently \
  --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=FIRST_SUPERUSER_PASSWORD="$(openssl rand -hex 16)" \
  --from-literal=POSTGRES_USER=experimently \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 32)" \
  --from-literal=AUDIT_HMAC_KEY="$(openssl rand -hex 32)"
```

## Install

`ingress.className=nginx` is ingress-nginx's IngressClass; name your cluster's.
`--wait` returns once the database schema is created and the API and dashboard are
ready.

<!-- chart-kind: install -->
```bash
helm install experimently "$CHART" --namespace experimently \
  --set profile="$PROFILE" \
  --set publicBaseUrl="$PUBLIC_BASE_URL" \
  --set firstSuperuser="$ADMIN_EMAIL" \
  --set existingSecret=experimently-secrets \
  --set ingress.className=nginx \
  --wait --timeout 10m
```

## Sign in as the first administrator

The first line prints `401`: the API answered through your Ingress, on your host, and
asks for a login. The last prints `ADMIN`, signed in with the password from the Secret.
In the first seconds after the install, ingress-nginx can still answer `503` while it
picks up the new pods; run the block again.

<!-- chart-kind: first-admin -->
```bash
curl -s -o /dev/null -w '%{http_code}\n' "$PUBLIC_BASE_URL/api/v1/experiments/"
ADMIN_PASSWORD=$(kubectl get secret experimently-secrets --namespace experimently \
  -o jsonpath='{.data.FIRST_SUPERUSER_PASSWORD}' | base64 -d)
jq -n --arg email "$ADMIN_EMAIL" --arg password "$ADMIN_PASSWORD" \
  '{email: $email, password: $password}' \
  | curl -s -X POST "$PUBLIC_BASE_URL/api/v1/auth/login" \
      -H 'content-type: application/json' -d @- \
  | jq -r .user.role
```

## Users' addresses

The API counts its rate limits per user address and writes that address on its
access lines. Through the chart's Ingress it reads the address from the
`X-Forwarded-For` header your ingress controller adds, and believes that header only
from the API pod's own network and the addresses and networks in
`api.forwardedAllowIps`. The chart's default is the private ranges,
`10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`, which covers an ingress controller on a
private pod network.

You can narrow it to the network your ingress controller's pods get their addresses
from, your cluster's pod network: `10.244.0.0/16` on kind. A single pod's address is
not enough, because a replaced pod gets another.
`kubectl get pods -n ingress-nginx -o wide -l app.kubernetes.io/component=controller`
lists ingress-nginx's controller pods with their addresses. Add
`--set api.forwardedAllowIps=<that network>` to the install or upgrade command. For
more than one network use a values file, or quote the whole argument and escape each
comma: `--set 'api.forwardedAllowIps=10.0.0.0/8\,192.168.0.0/16'`. A controller that
runs with `hostNetwork: true` connects from its node's address; give your nodes'
network then.

To check it once people have used the dashboard, count the addresses on the API's
access lines, as [Users' addresses behind a proxy](proxies.md#checking-it) shows.
Your users' addresses mean it works. One pod address on every line is your ingress
controller's: `api.forwardedAllowIps` does not cover it. Node or load balancer
addresses on every line mean the controller itself does not receive your users'
addresses; its documentation says how to pass them on.

## Upgrade

Point `CHART` at the new chart. The values given at install are kept.

Upgrade the chart together with the image. Earlier charts left
`api.forwardedAllowIps` empty, and under one of them a newer image believes
`X-Forwarded-For` only from the API pod's own network. The chart now refuses an
empty `api.forwardedAllowIps`: `--reset-then-reuse-values`, below, takes the new
default, while `--reuse-values` can keep an earlier chart's empty value, and that
upgrade fails naming `api.forwardedAllowIps`. If your own values set it to `""`,
give it a network on this upgrade, such as `--set api.forwardedAllowIps=10.244.0.0/16`
on kind.

<!-- chart-kind: upgrade -->
```bash
helm upgrade experimently "$CHART" --namespace experimently \
  --reset-then-reuse-values \
  --wait --timeout 10m
```
