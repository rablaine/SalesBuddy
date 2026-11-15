@description('Independent synthetic experiment. Never redeploys the email app or APIM.')
param location string = 'centralus'
param functionName string = 'func-salesbuddy-failure-lab'
param storageName string = 'stsalesbuddyfixlab'
param planName string = 'plan-salesbuddy-failure-lab'
param diagnosticsStorageName string = 'stsalesbuddydiag'
param diagnosticsContainerName string = 'diagnostics'
param vnetName string = 'vnet-salesbuddy-diagnostics'
param testRepository string
param syntheticReportId string
param syntheticContentHash string
param vaultName string = 'kvsalesbuddyfixlab'
param operatorPrincipalId string

var blobOwnerRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
var blobReaderRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1')
var queueRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '974c5e8b-45b9-4653-ba55-5f855dd0fb88')
var storageRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '17d1049b-9a84-46fb-8f53-869881c3d3ab')
var secretsUserRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var secretsOfficerRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: vnetName
}

resource diagnosticStorage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: diagnosticsStorageName
}
resource diagnosticBlobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: diagnosticStorage
  name: 'default'
}
resource diagnosticReports 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = {
  parent: diagnosticBlobs
  name: diagnosticsContainerName
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    publicNetworkAccess: 'Disabled'
    networkAcls: { defaultAction: 'Deny', bypass: 'None' }
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {}
}
resource deployment 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'deployment'
  properties: { publicAccess: 'None' }
}
resource state 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'experiments'
  properties: { publicAccess: 'None' }
}

var services = ['blob', 'queue', 'table']
resource dns 'Microsoft.Network/privateDnsZones@2024-06-01' existing = [for service in services: {
  name: 'privatelink.${service}.${environment().suffixes.storage}'
}]
resource endpoints 'Microsoft.Network/privateEndpoints@2024-05-01' = [for service in services: {
  name: 'pe-salesbuddy-failure-lab-${service}'
  location: location
  properties: {
    subnet: { id: '${vnet.id}/subnets/private-endpoints' }
    privateLinkServiceConnections: [{
      name: service
      properties: { privateLinkServiceId: storage.id, groupIds: [service] }
    }]
  }
}]
resource dnsGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = [for (service, i) in services: {
  parent: endpoints[i]
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [{
      name: service
      properties: { privateDnsZoneId: dns[i].id }
    }]
  }
}]

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: planName
  location: location
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: { reserved: true }
}
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: vaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    accessPolicies: []
    publicNetworkAccess: 'Disabled'
  }
}
resource function 'Microsoft.Web/sites@2024-04-01' = {
  name: functionName
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    virtualNetworkSubnetId: '${vnet.id}/subnets/functions'
    functionAppConfig: {
      runtime: { name: 'python', version: '3.11' }
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deployment.name}'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: { maximumInstanceCount: 1, instanceMemoryMB: 2048 }
    }
    siteConfig: {
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'REPORT_STORAGE_URL', value: diagnosticStorage.properties.primaryEndpoints.blob }
        { name: 'REPORT_CONTAINER', value: diagnosticsContainerName }
        { name: 'EXPERIMENT_STORAGE_URL', value: storage.properties.primaryEndpoints.blob }
        { name: 'EXPERIMENT_CONTAINER', value: state.name }
        { name: 'INVESTIGATION_ENABLED', value: 'false' }
        { name: 'INVESTIGATION_MODE', value: 'preview' }
        { name: 'GITHUB_TEST_REPOSITORY', value: testRepository }
        { name: 'SYNTHETIC_REPORT_ID', value: syntheticReportId }
        { name: 'SYNTHETIC_CONTENT_HASH', value: syntheticContentHash }
        {
          name: 'GITHUB_TOKEN'
          value: '@Microsoft.KeyVault(SecretUri=${vault.properties.vaultUri}secrets/github-test-token/)'
        }
      ]
    }
  }
  dependsOn: [dnsGroups]
}

resource reportReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(diagnosticReports.id, function.id, blobReaderRole)
  scope: diagnosticReports
  properties: {
    principalId: function.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: blobReaderRole
  }
}
resource hostBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, function.id, blobOwnerRole)
  scope: storage
  properties: {
    principalId: function.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: blobOwnerRole
  }
}
resource hostQueueRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, function.id, queueRole)
  scope: storage
  properties: {
    principalId: function.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: queueRole
  }
}
resource hostStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, function.id, storageRole)
  scope: storage
  properties: {
    principalId: function.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageRole
  }
}
resource tokenReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, function.id, secretsUserRole)
  scope: vault
  properties: {
    principalId: function.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: secretsUserRole
  }
}
resource tokenOperator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, operatorPrincipalId, secretsOfficerRole)
  scope: vault
  properties: {
    principalId: operatorPrincipalId
    principalType: 'User'
    roleDefinitionId: secretsOfficerRole
  }
}

output functionHost string = function.properties.defaultHostName
output functionPrincipalId string = function.identity.principalId
output tokenVaultName string = vault.name
