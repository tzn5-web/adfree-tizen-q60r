// DEPLOY ONLY AFTER OWNER CONSENT AND COST REVIEW.
// Usage: az deployment group create -g rg-chatgpt-azure -f azure.bicep -p storageName=<globallyUniqueName>
// Standard_LRS, private and restricted to Entra authorization for data plane.
@description('Unique lowercase Azure storage account name (3-24 characters).')
@minLength(3)
@maxLength(24)
param storageName string

param location string = resourceGroup().location

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    networkAcls: { defaultAction: 'Allow', bypass: 'AzureServices' }
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {}
}

resource audit 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'device-control-audit'
  properties: { publicAccess: 'None' }
}

output blobEndpoint string = storage.properties.primaryEndpoints.blob

resource projects 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'device-control-projects'
  properties: { publicAccess: 'None' }
}
