// Azure infrastructure for the AI Gateway.
//
// STATUS: THIS TEMPLATE HAS NEVER BEEN DEPLOYED. There is no Azure CLI and no
// subscription in the environment it was written in. It is a design artefact:
// it expresses the intended topology and the security decisions, and it has
// not been validated against ARM. Do not claim it is running anywhere.
//
// Deploy with:
//   az group create -n rg-llm-gateway -l uksouth
//   az deployment group create -g rg-llm-gateway -f infra/main.bicep \
//      -p containerImage=<acr>.azurecr.io/llm-gateway:<tag>
//
// THE CENTRAL SECURITY DECISION
//   No secret is ever a template parameter with a value, an app setting, or a
//   layer in the image. Secrets live in Key Vault; the Container App reads
//   them through a USER-ASSIGNED MANAGED IDENTITY. That means there is no
//   bootstrap credential to rotate, nothing sensitive in deployment history
//   (which is readable by anyone with reader access to the resource group),
//   and no secret in `az deployment group show`.

targetScope = 'resourceGroup'

@description('Base name; every resource derives from it.')
param name string = 'llm-gateway'

@description('Location for all resources.')
param location string = resourceGroup().location

@description('Fully qualified container image, e.g. myacr.azurecr.io/llm-gateway:v1.')
param containerImage string

@description('Minimum replicas. 0 enables scale-to-zero at the cost of cold starts.')
@minValue(0)
param minReplicas int = 1

@description('Maximum replicas. Bounds cost as well as load.')
@minValue(1)
param maxReplicas int = 5

@description('Router strategy: order | cost | latency.')
@allowed(['order', 'cost', 'latency'])
param routingStrategy string = 'cost'

// Suffix keeps globally-unique names (Key Vault, ACR) distinct per deployment
// without requiring the operator to invent one.
var suffix = uniqueString(resourceGroup().id)
var keyVaultName = take('kv-${name}-${suffix}', 24)

// --- identity ---------------------------------------------------------------
// User-assigned rather than system-assigned, deliberately: the identity
// outlives the Container App, so the Key Vault role assignment survives the app
// being deleted and recreated. With a system-assigned identity, every redeploy
// mints a new principal and the access grant has to be re-applied.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${name}'
  location: location
}

// --- secrets ----------------------------------------------------------------
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    // RBAC rather than legacy access policies: it is the current model, and it
    // makes the grant below auditable through the same role-assignment surface
    // as everything else in the subscription.
    enableRbacAuthorization: true
    // Protects against an accidental (or malicious) delete removing every
    // provider credential with no recovery path.
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled' // Private endpoint would be the hardening step.
  }
}

// Built-in "Key Vault Secrets User": read secret VALUES, nothing else. Not
// Contributor, not Officer — the app never needs to create, list-with-values,
// or delete a secret, and granting more than it needs is how a compromised
// container becomes a compromised vault.
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource secretsAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, identity.id, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      keyVaultSecretsUserRoleId
    )
    principalId: identity.properties.principalId
    // Required: without it, deployment can fail on a race where the identity's
    // principal has not yet propagated to Azure AD.
    principalType: 'ServicePrincipal'
  }
}

// NOTE: the secrets themselves are NOT created here, on purpose. Putting a
// secret value in a template means putting it in deployment history. Create
// them out of band, once:
//   az keyvault secret set --vault-name <kv> -n anthropic-api-key --value <...>
//   az keyvault secret set --vault-name <kv> -n gateway-api-keys  --value <...>

// --- observability ----------------------------------------------------------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${name}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    // The audit log (Phase 6) lands here via stdout. Retention is a compliance
    // parameter, not a technical one — 30 days is a starting point to be set
    // against an actual policy.
    retentionInDays: 30
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'env-${name}'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// --- the app ----------------------------------------------------------------
resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-${name}'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  // Explicit dependency: without the role assignment in place first, the app
  // starts, fails to read its secrets, and crash-loops.
  dependsOn: [secretsAccess]
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        // The gateway's own bearer auth is the access control (Phase 6);
        // ingress is transport, not authorization.
        transport: 'auto'
        allowInsecure: false
      }
      // Container Apps resolves these from Key Vault using the managed
      // identity. The VALUES never appear in this template, in deployment
      // history, or in `az containerapp show`.
      secrets: [
        {
          name: 'anthropic-api-key'
          keyVaultUrl: '${keyVault.properties.vaultUri}secrets/anthropic-api-key'
          identity: identity.id
        }
        {
          name: 'gateway-api-keys'
          keyVaultUrl: '${keyVault.properties.vaultUri}secrets/gateway-api-keys'
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: name
          image: containerImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            // Secret references — the literal value is never in the template.
            { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }
            { name: 'GATEWAY_API_KEYS', secretRef: 'gateway-api-keys' }
            // Non-secret configuration, plainly visible on purpose.
            { name: 'ROUTING_STRATEGY', value: routingStrategy }
            { name: 'LOG_LEVEL', value: 'INFO' }
            // Empty = stdout, which Container Apps ships to Log Analytics.
            { name: 'AUDIT_LOG_PATH', value: '' }
            // Stays false in Azure: prompt text would land in Log Analytics
            // with its retention and access model. Turning this on is a data
            // protection decision, not a debugging convenience.
            { name: 'AUDIT_LOG_CONTENT', value: 'false' }
            { name: 'SELF_BASE_URL', value: 'https://ca-${name}.${environment.properties.defaultDomain}' }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8000 }
              initialDelaySeconds: 15
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/healthz', port: 8000 }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: { metadata: { concurrentRequests: '20' } }
          }
        ]
      }
    }
  }
}

output gatewayUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output keyVaultName string = keyVault.name
output identityPrincipalId string = identity.properties.principalId

// REMINDER: replicas do not share state. The Phase 6 rate limiter and the
// Phase 2 latency tracker are both per-process, so maxReplicas > 1 means the
// effective rate limit is maxReplicas x RATE_LIMIT_PER_MINUTE. Documented in
// docs/design-decisions.md; moving them to Redis is the fix.
