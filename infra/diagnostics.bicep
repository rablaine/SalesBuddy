@description('Dedicated diagnostics intake. Does not alter existing AI API routes.')
param location string = 'centralus'
param functionName string = 'func-salesbuddy-diag-private'
param storageName string = 'stsalesbuddydiag'
param consumptionPlanName string = 'plan-salesbuddy-diag-flex'
param apimPrincipalId string
param applicationId string
param supportEmail string
param vnetName string = 'vnet-salesbuddy-diagnostics'
param acsSender string = 'DoNotReply@92ef03a8-d4ee-42f7-bb16-6df64fed76c0.azurecomm.net'

var tenantId = subscription().tenantId
var blobOwnerRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
var queueRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '974c5e8b-45b9-4653-ba55-5f855dd0fb88')
var storageRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '17d1049b-9a84-46fb-8f53-869881c3d3ab')

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
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
    }
  }
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'deployment'
  properties: { publicAccess: 'None' }
}

resource queues 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {}
}

resource diagnosticQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-05-01' = {
  parent: queues
  name: 'diagnostics'
  properties: {}
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: { addressPrefixes: ['10.92.0.0/16'] }
    subnets: [
      {
        name: 'functions'
        properties: {
          addressPrefix: '10.92.0.0/24'
          delegations: [{
            name: 'flex'
            properties: { serviceName: 'Microsoft.App/environments' }
          }]
        }
      }
      {
        name: 'private-endpoints'
        properties: {
          addressPrefix: '10.92.1.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

var storageServices = ['blob', 'queue', 'table']
resource dns 'Microsoft.Network/privateDnsZones@2024-06-01' = [for service in storageServices: {
  name: 'privatelink.${service}.${environment().suffixes.storage}'
  location: 'global'
}]

resource dnsLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (service, i) in storageServices: {
  parent: dns[i]
  name: '${vnetName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}]

resource endpoints 'Microsoft.Network/privateEndpoints@2024-05-01' = [for service in storageServices: {
  name: 'pe-salesbuddy-diag-${service}'
  location: location
  properties: {
    subnet: { id: '${vnet.id}/subnets/private-endpoints' }
    privateLinkServiceConnections: [{
      name: service
      properties: {
        privateLinkServiceId: storage.id
        groupIds: [service]
      }
    }]
  }
}]

resource dnsGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = [for (service, i) in storageServices: {
  parent: endpoints[i]
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [{
      name: service
      properties: { privateDnsZoneId: dns[i].id }
    }]
  }
}]

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {}
}

resource reports 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'diagnostics'
  properties: { publicAccess: 'None' }
}

resource retention 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [{
        enabled: true
        name: 'diagnostic-retention'
        type: 'Lifecycle'
        definition: {
          filters: { blobTypes: ['blockBlob'], prefixMatch: ['diagnostics/'] }
          actions: { baseBlob: { delete: { daysAfterModificationGreaterThan: 30 } } }
        }
      }]
    }
  }
}

resource email 'Microsoft.Communication/emailServices@2023-03-31' = {
  name: 'salesbuddy-diagnostics-email'
  location: 'global'
  properties: { dataLocation: 'United States' }
}

resource domain 'Microsoft.Communication/emailServices/domains@2023-03-31' = {
  parent: email
  name: 'AzureManagedDomain'
  location: 'global'
  properties: {
    domainManagement: 'AzureManaged'
    userEngagementTracking: 'Disabled'
  }
}

resource communication 'Microsoft.Communication/communicationServices@2023-03-31' = {
  name: 'salesbuddy-diagnostics-mail'
  location: 'global'
  properties: {
    dataLocation: 'United States'
    linkedDomains: [domain.id]
  }
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: consumptionPlanName
  location: location
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: { reserved: true }
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
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainer.name}'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
    }
    siteConfig: {
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'REPORT_STORAGE_URL', value: storage.properties.primaryEndpoints.blob }
        { name: 'REPORT_CONTAINER', value: reports.name }
        { name: 'REPORT_QUEUE__queueServiceUri', value: storage.properties.primaryEndpoints.queue }
        { name: 'REPORT_QUEUE__credential', value: 'managedidentity' }
        { name: 'DIAGNOSTICS_QUEUE_NAME', value: diagnosticQueue.name }
        { name: 'APIM_PRINCIPAL_ID', value: apimPrincipalId }
        { name: 'SUPPORT_EMAIL', value: supportEmail }
        { name: 'ACS_ENDPOINT', value: 'https://${communication.properties.hostName}' }
        { name: 'ACS_SENDER', value: acsSender }
      ]
    }
  }
  dependsOn: [dnsGroups, dnsLinks]
}

resource auth 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: function
  name: 'authsettingsV2'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'Return401'
    }
    httpSettings: { requireHttps: true }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: applicationId
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: ['api://${applicationId}', applicationId]
          defaultAuthorizationPolicy: {
            allowedPrincipals: { identities: [apimPrincipalId] }
          }
        }
      }
    }
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

resource mailRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(communication.id, function.id, 'email-sender')
  scope: communication
  properties: {
    principalId: function.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c')
  }
}

output functionPrincipalId string = function.identity.principalId
output functionHost string = function.properties.defaultHostName
output applicationAudience string = 'api://${applicationId}'
output mailHost string = communication.properties.hostName
output senderDomain string = domain.properties.mailFromSenderDomain
output virtualNetworkId string = vnet.id
