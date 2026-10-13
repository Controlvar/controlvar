# Architecture on AWS

The components, and where each request goes, are in the
[Architecture Overview](../getting-started/architecture.md). Every stack the CDK app
creates, for each profile, is in
[What Gets Deployed](../self-hosting/cdk.md#what-gets-deployed). This page says how a
request reaches the API and the dashboard on AWS.

## Request routing

-   **Application Load Balancer**: For routing traffic to backend services. This is the only entry point the CDK creates: HTTPS, with HTTP redirected to HTTPS, in front of the API.

**The dashboard is its own ECS service behind the same load balancer**: the
HTTPS listener sends `/api/*`, `/health`, `/health/*` and `/metrics` to the
API and everything else to the dashboard. `cdk deploy` starts it on the
`experimentation-platform/web:bootstrap` image; the Deploy workflow rolls
each release onto it by digest, after the API. No stack creates CloudFront
or API Gateway. The dashboard is a static Next.js export served by nginx; in
Docker Compose that nginx also proxies `/api/` to the API, and in AWS it
proxies nothing.
