@description('Add only a vault endpoint and new DNS link for the existing lab worker VNet.')
param location string

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: 'kvsalesbuddyfixlab'
}
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: 'vnet-salesbuddy-diagnostics'
}

resource dns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.vaultcore.azure.net'
  location: 'global'
}
resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: dns
  name: 'failure-lab-vault'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}
resource endpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-salesbuddy-failure-lab-vault'
  location: location
  properties: {
    subnet: { id: '${vnet.id}/subnets/private-endpoints' }
    privateLinkServiceConnections: [{
      name: 'vault'
      properties: {
        privateLinkServiceId: vault.id
        groupIds: ['vault']
      }
    }]
  }
}
resource zoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [{
      name: 'vault'
      properties: { privateDnsZoneId: dns.id }
    }]
  }
}
