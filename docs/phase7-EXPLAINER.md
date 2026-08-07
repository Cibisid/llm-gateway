# Phase 7 — Container, Azure IaC, and CI

## Honest status first

**The service has not been deployed to Azure.** There is no Azure CLI and no
subscription in the environment this was built in, and provisioning billable
cloud resources is not something to do unasked. `infra/main.bicep` is a design
artefact: it expresses the topology and the security decisions, and it has
**never been validated against ARM**.

What *is* real: the Dockerfile, the `.dockerignore`, and the CI workflow — and
CI verifies the container both **builds and serves**, which is the check that
matters.

## The Dockerfile

Multi-stage, so the runtime image carries no build toolchain: pip, wheels, and
compilers are attack surface and image size a running service has no use for.

**Non-root.** A container process running as root that gets compromised through
a dependency has root *inside* the container — a much shorter path to host
escape than an unprivileged one. CI asserts this rather than trusting it:

```yaml
user=$(docker run --rm --entrypoint id llm-gateway:sha -un)
if [ "$user" = "root" ]; then exit 1; fi
```

**Python 3.12, not 3.14.** Local development runs 3.14 and the code supports
both. The image pins the conservative choice because 3.12 has uniformly
available wheels for this dependency set, so the build needs no compiler.

**No secrets baked in.** Every credential arrives at runtime from the
environment. An image is not a secret store: it is pushed to a registry, cached
on every node that runs it, and its layers are readable by anyone who can pull
it.

`.dockerignore` excludes `.env` explicitly. That is not redundant with
`.gitignore` — git and Docker are separate systems, and excluding a file from
version control does nothing for the build context.

## The Bicep, and its one real decision

**No secret is ever a template parameter, an app setting, or an image layer.**
Secrets live in Key Vault; the Container App reads them through a
**user-assigned managed identity**. So there is no bootstrap credential to
rotate, and nothing sensitive in deployment history — which is readable by
anyone with reader access to the resource group, a detail people routinely
forget.

Three supporting choices:

- **User-assigned, not system-assigned identity.** The identity outlives the
  Container App, so the Key Vault role assignment survives the app being deleted
  and recreated. A system-assigned identity mints a new principal on every
  recreate and the grant has to be re-applied.
- **`Key Vault Secrets User`, not Contributor.** The app reads secret values and
  does nothing else. Granting more is how a compromised container becomes a
  compromised vault.
- **The secrets are not created by the template.** Putting a value in a template
  puts it in deployment history. They are created out of band, once.

`AUDIT_LOG_CONTENT` is pinned to `false` in Azure: prompt text would otherwise
land in Log Analytics with its retention and access model. Turning it on is a
data-protection decision, not a debugging convenience.

## CI

Three jobs, and the interesting thing is what each one refuses to accept.

**test** — unit tests, then the offline evaluation, then the model evaluation.
The offline eval runs on **every push from anyone, including forks**, because it
needs no key. That split is the whole reason Phase 5 separates offline from
online: a check that only runs where secrets exist does not protect a pull
request. Where a key *does* exist, the step uses `--require-online`, which turns
a skipped model case into a **failure** — a silent skip would mean the model
checks quietly stopped running.

**security** — fails if `.env` is tracked, and greps **all of git history** for
secret-shaped strings. History matters: a secret committed and later removed is
still leaked and still needs rotating. The pattern matches real provider key
formats rather than the word "key", so it fails on leaks instead of on
documentation.

**container** — builds the image, then **runs it and curls `/healthz`**.
Building only proves it compiles; an image that builds and then crash-loops
passes that check. Then it asserts the container is not running as root.

## Verification

- Dockerfile, `.dockerignore`, and `ci.yml` are written and committed.
- **The local image build could not be verified**: Docker Desktop's Linux engine
  did not come up in this environment. The build is exercised by the `container`
  CI job on first push, which is where it will be confirmed.
- The Bicep is unvalidated — no Azure CLI, no subscription.

## To actually deploy

```bash
az group create -n rg-llm-gateway -l uksouth
```

```bash
az deployment group create -g rg-llm-gateway -f infra/main.bicep -p containerImage=<acr>.azurecr.io/llm-gateway:v1
```

Then set the secrets in the vault the template created:

```bash
az keyvault secret set --vault-name <kv-name> -n anthropic-api-key --value <your-key>
```

**Before scaling past one replica:** the rate limiter and latency tracker are
per-process, so `maxReplicas: 5` means five times the configured rate limit.
Move both to Redis first.
