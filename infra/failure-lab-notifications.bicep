@description('Existing verified domain, linked read-only by the new lab sending endpoint.')
param verifiedDomainId string
param labPrincipalId string

resource mail 'Microsoft.Communication/communicationServices@2023-03-31' = {
  name: 'salesbuddy-failure-lab-mail'
  location: 'global'
  properties: {
    dataLocation: 'United States'
    linkedDomains: [verifiedDomainId]
  }
}

resource senderRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(mail.id, labPrincipalId, 'completion-email-sender')
  scope: mail
  properties: {
    principalId: labPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c')
  }
}

output endpoint string = 'https://${mail.properties.hostName}'
