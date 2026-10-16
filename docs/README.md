# Controlvar Documentation

Open-source A/B testing and feature flags that you run yourself. The core, the optional modules
and the dashboard are Apache-2.0; the client SDKs are MIT.

## Start Here

| Guide | What it covers |
|-------|----------------|
| [Quick Start](getting-started/quick-start.md) | Start the local stack with Docker, then create, start and read an experiment, an API key and a feature flag |
| [Demo guide](https://github.com/Controlvar/controlvar/blob/main/demo/DEMO_GUIDE.md) | ShopLab and StreamPulse, two sample apps that `./demo/setup-local.sh` starts with the platform |
| [Limitations](getting-started/limitations.md) | What Controlvar does not do, and what happens instead |

---

## Getting Started

- [Core Concepts](getting-started/concepts.md) — experiments, variants, metrics, feature flags and targeting
- [Architecture Overview](getting-started/architecture.md) — the components and where each request goes
- [FAQ](getting-started/faq.md)
- [Modules & Profiles](getting-started/modules.md) — the core and full profiles, and what each module adds

---

## Self-Hosting

- [Docker Compose on one host](https://github.com/Controlvar/controlvar/blob/main/deploy/compose/compose.yml) — the self-hosting stack, on the published images
- [Kubernetes (Helm)](self-hosting/kubernetes.md)
- [AWS CDK Deployment](self-hosting/cdk.md)

---

## SDKs

- [JavaScript SDK](sdk/javascript.md), [Python SDK](sdk/python.md) and [Go SDK](sdk/go.md)
- [SDK contract & status](sdk-guide.md) — every SDK, how to install it, and what CI runs for it

---

## API Reference

- [Using the API Docs](api/api-docs-guide.md) — the reference the server serves at `/api/v1/docs`
- [Experiments](api/endpoints.md) and [Authentication](api/auth.md)
