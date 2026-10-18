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

## Upgrade

Point `CHART` at the new chart. The values given at install are kept.

<!-- chart-kind: upgrade -->
```bash
helm upgrade experimently "$CHART" --namespace experimently \
  --reset-then-reuse-values \
  --wait --timeout 10m
```
