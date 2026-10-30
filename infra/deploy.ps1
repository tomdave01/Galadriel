$ErrorActionPreference = "Stop"

# Edit these values before deployment. FoundryProjectScope is the ARM resource ID
# of the Foundry project, not the project endpoint URL.
$ResourceGroupName = "overseer-rg"
$Region = "eastus2"
$ContainerAppName = "overseer-api"
$StaticWebAppName = "overseer-web"
$FoundryProjectScope = "/subscriptions/d6e4e6dc-d576-4acd-955e-865ce88d5452/resourceGroups/overseer-rg/providers/Microsoft.CognitiveServices/accounts/Overseer1-resource/projects/Overseer1"
$FoundryUserRoleDefinitionId = "53ca6127-db72-4b80-b1b0-d745d6d5456d"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DotEnvPath = Join-Path $RepoRoot ".env"

function Get-DeploymentSetting {
    param([Parameter(Mandatory = $true)][string]$Name)

    $processValue = [Environment]::GetEnvironmentVariable($Name, "Process")
    if (-not [string]::IsNullOrWhiteSpace($processValue)) {
        $processValue = $processValue.Trim()
        if ($processValue.StartsWith("<") -and $processValue.EndsWith(">")) {
            return ""
        }
        return $processValue
    }

    if (-not (Test-Path -LiteralPath $DotEnvPath -PathType Leaf)) {
        return ""
    }

    foreach ($line in Get-Content -LiteralPath $DotEnvPath) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*)\s*$") {
            $value = $Matches[1].Trim()
            if ($value.Length -ge 2) {
                $first = $value.Substring(0, 1)
                $last = $value.Substring($value.Length - 1, 1)
                if (($first -eq '"' -and $last -eq '"') -or
                    ($first -eq "'" -and $last -eq "'")) {
                    $value = $value.Substring(1, $value.Length - 2)
                }
            }
            if ($value.StartsWith("<") -and $value.EndsWith(">")) {
                return ""
            }
            return $value
        }
    }
    return ""
}

function Invoke-Az {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$CaptureOutput
    )

    # Use Continue so that az WARNING/INFO lines written to stderr do not
    # trigger a terminating error under $ErrorActionPreference = "Stop".
    # Exit-code checking below is the authoritative failure signal.
    $local:ErrorActionPreference = "Continue"
    $output = & az @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $commandName = ($Arguments | Select-Object -First 2) -join " "
        $errorDetail = ($output | Where-Object { $_ -match "ERROR:" }) -join [Environment]::NewLine
        if ([string]::IsNullOrWhiteSpace($errorDetail)) {
            $errorDetail = ($output -join [Environment]::NewLine).Trim()
        }
        throw "Azure CLI command 'az $commandName' failed with exit code $exitCode.`n$errorDetail"
    }
    if ($CaptureOutput) {
        return ($output -join [Environment]::NewLine).Trim()
    }
}

function Invoke-SwaDeploy {
    param([Parameter(Mandatory = $true)][string]$ContentPath)

    & swa deploy $ContentPath --env production
    if ($LASTEXITCODE -ne 0) {
        throw "Static Web Apps CLI deployment failed with exit code $LASTEXITCODE."
    }
}

if ([string]::IsNullOrWhiteSpace($FoundryProjectScope) -or
    $FoundryProjectScope -notmatch "^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.CognitiveServices/accounts/[^/]+/projects/[^/]+/?$") {
    throw "Set FoundryProjectScope at the top of this script to the Foundry project's complete Azure resource ID."
}

$FoundryProjectEndpoint = Get-DeploymentSetting "FOUNDRY_PROJECT_ENDPOINT"
$FoundryModel = Get-DeploymentSetting "FOUNDRY_MODEL"
$ApplicationInsightsConnectionString = Get-DeploymentSetting "APPLICATIONINSIGHTS_CONNECTION_STRING"

if (-not $FoundryProjectEndpoint.StartsWith("https://")) {
    throw "FOUNDRY_PROJECT_ENDPOINT must be set to an HTTPS Foundry project endpoint in .env or the process environment."
}
if ($FoundryProjectEndpoint -notmatch "^https://[^/]+/api/projects/[^/]+/?$") {
    throw "FOUNDRY_PROJECT_ENDPOINT must use the Foundry project endpoint form https://<resource>.services.ai.azure.com/api/projects/<project>."
}
if ([string]::IsNullOrWhiteSpace($FoundryModel)) {
    throw "FOUNDRY_MODEL must be set to the Foundry deployment name in .env or the process environment."
}
if ($FoundryModel.StartsWith("<") -and $FoundryModel.EndsWith(">")) {
    throw "FOUNDRY_MODEL must be the actual Foundry deployment name, not a placeholder."
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI (az) is required. Install it and run 'az login' before deployment."
}
if (-not (Get-Command swa -ErrorAction SilentlyContinue)) {
    throw "Static Web Apps CLI (swa) is required. Install it with 'npm install -g @azure/static-web-apps-cli'."
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "Node.js and npm are required to install and run the Static Web Apps CLI."
}
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot "Dockerfile") -PathType Leaf)) {
    throw "Dockerfile was not found in the repository root."
}

$null = Invoke-Az @("account", "show", "--only-show-errors", "--output", "none")

Write-Host "Adding or upgrading the Azure Container Apps CLI extension..."
$null = Invoke-Az @(
    "extension", "add", "--name", "containerapp", "--upgrade",
    "--only-show-errors", "--output", "none"
)

Write-Host "Creating the resource group if needed..."
$null = Invoke-Az @(
    "group", "create", "--name", $ResourceGroupName, "--location", $Region,
    "--only-show-errors", "--output", "none"
)

# ACR Tasks are blocked by subscription policy, so we build the image locally,
# push it to ACR, and deploy with --image instead of --source.
# Derive a deterministic, registry-name-safe ACR name (alphanumeric, 5-50 chars).
$AcrName = ($ResourceGroupName -replace "[^a-zA-Z0-9]", "").ToLower() + "acr"
if ($AcrName.Length -gt 50) { $AcrName = $AcrName.Substring(0, 50) }

Write-Host "Creating the Azure Container Registry if needed..."
$null = Invoke-Az @(
    "acr", "create",
    "--name", $AcrName,
    "--resource-group", $ResourceGroupName,
    "--sku", "Basic",
    "--admin-enabled", "true",
    "--only-show-errors", "--output", "none"
)

Write-Host "Logging in to ACR..."
$local:ErrorActionPreference = "Continue"
& az acr login --name $AcrName
if ($LASTEXITCODE -ne 0) { throw "ACR login failed." }

$ImageTag = "$AcrName.azurecr.io/${ContainerAppName}:latest"

Write-Host "Building the Docker image locally..."
Push-Location $RepoRoot
try {
    $local:ErrorActionPreference = "Continue"
    & docker build -t $ImageTag .
    if ($LASTEXITCODE -ne 0) { throw "docker build failed." }

    Write-Host "Pushing the image to ACR..."
    & docker push $ImageTag
    if ($LASTEXITCODE -ne 0) { throw "docker push failed." }
}
finally {
    Pop-Location
}

Write-Host "Deploying the backend Container App..."
$null = Invoke-Az @(
    "containerapp", "up",
    "--name", $ContainerAppName,
    "--resource-group", $ResourceGroupName,
    "--location", $Region,
    "--image", $ImageTag,
    "--registry-server", "$AcrName.azurecr.io",
    "--ingress", "external",
    "--target-port", "8000",
    "--env-vars",
    "FOUNDRY_PROJECT_ENDPOINT=$FoundryProjectEndpoint",
    "FOUNDRY_MODEL=$FoundryModel",
    "APPLICATIONINSIGHTS_CONNECTION_STRING=$ApplicationInsightsConnectionString"
)

Write-Host "Assigning a system-managed identity and granting Foundry project access..."
$null = Invoke-Az @(
    "containerapp", "identity", "assign",
    "--name", $ContainerAppName,
    "--resource-group", $ResourceGroupName,
    "--system-assigned",
    "--only-show-errors", "--output", "none"
)
$principalId = Invoke-Az @(
    "containerapp", "show",
    "--name", $ContainerAppName,
    "--resource-group", $ResourceGroupName,
    "--query", "identity.principalId", "--output", "tsv",
    "--only-show-errors"
) -CaptureOutput
if ([string]::IsNullOrWhiteSpace($principalId)) {
    throw "Azure did not return the Container App system identity principal ID."
}
$existingRoleAssignment = Invoke-Az @(
    "role", "assignment", "list",
    "--assignee-object-id", $principalId,
    "--scope", $FoundryProjectScope,
    "--role", $FoundryUserRoleDefinitionId,
    "--query", "[0].id", "--output", "tsv",
    "--only-show-errors"
) -CaptureOutput
if ([string]::IsNullOrWhiteSpace($existingRoleAssignment)) {
    # UNVERIFIED: Current Microsoft Learn guidance describes Foundry User
    # assignment on the Foundry resource; this uses the requested project scope.
    $null = Invoke-Az @(
        "role", "assignment", "create",
        "--assignee-object-id", $principalId,
        "--assignee-principal-type", "ServicePrincipal",
        "--role", $FoundryUserRoleDefinitionId,
        "--scope", $FoundryProjectScope,
        "--only-show-errors", "--output", "none"
    )
}

$apiHost = Invoke-Az @(
    "containerapp", "show",
    "--name", $ContainerAppName,
    "--resource-group", $ResourceGroupName,
    "--query", "properties.configuration.ingress.fqdn", "--output", "tsv",
    "--only-show-errors"
) -CaptureOutput
if ([string]::IsNullOrWhiteSpace($apiHost)) {
    throw "Azure did not return the Container App ingress hostname."
}
$apiOrigin = "https://$apiHost"

Write-Host "Creating the Free Azure Static Web App..."
$null = Invoke-Az @(
    "staticwebapp", "create",
    "--name", $StaticWebAppName,
    "--resource-group", $ResourceGroupName,
    "--location", $Region,
    "--sku", "Free",
    "--only-show-errors", "--output", "none"
)
$staticHost = Invoke-Az @(
    "staticwebapp", "show",
    "--name", $StaticWebAppName,
    "--resource-group", $ResourceGroupName,
    "--query", "defaultHostname", "--output", "tsv",
    "--only-show-errors"
) -CaptureOutput
if ([string]::IsNullOrWhiteSpace($staticHost)) {
    throw "Azure did not return the Static Web App hostname."
}
$staticOrigin = "https://$staticHost"

Write-Host "Restricting API CORS to the Static Web App origin..."
$null = Invoke-Az @(
    "containerapp", "update",
    "--name", $ContainerAppName,
    "--resource-group", $ResourceGroupName,
    "--set-env-vars", "CORS_ALLOWED_ORIGINS=$staticOrigin",
    "--only-show-errors", "--output", "none"
)
$null = Invoke-Az @(
    "containerapp", "ingress", "cors", "enable",
    "--name", $ContainerAppName,
    "--resource-group", $ResourceGroupName,
    "--allowed-origins", $staticOrigin,
    "--allowed-methods", "GET", "POST", "OPTIONS",
    "--allowed-headers", "Content-Type", "Last-Event-ID",
    "--only-show-errors", "--output", "none"
)

Write-Host "Deploying the web folder with the Static Web Apps CLI..."
$deploymentToken = Invoke-Az @(
    "staticwebapp", "secrets", "list",
    "--name", $StaticWebAppName,
    "--resource-group", $ResourceGroupName,
    "--query", "properties.apiKey", "--output", "tsv",
    "--only-show-errors"
) -CaptureOutput
if ([string]::IsNullOrWhiteSpace($deploymentToken)) {
    throw "Azure did not return a Static Web Apps deployment token."
}

$stagingPath = Join-Path ([System.IO.Path]::GetTempPath()) (
    "overseer-web-" + [guid]::NewGuid().ToString("N")
)
$previousDeploymentToken = $env:SWA_CLI_DEPLOYMENT_TOKEN
try {
    New-Item -ItemType Directory -Path $stagingPath | Out-Null
    $webFiles = Get-ChildItem -LiteralPath (Join-Path $RepoRoot "web") -Force
    foreach ($webFile in $webFiles) {
        Copy-Item -LiteralPath $webFile.FullName -Destination $stagingPath -Recurse
    }
    $configValue = ConvertTo-Json -InputObject $apiOrigin -Compress
    $configPath = Join-Path $stagingPath "config.js"
    [System.IO.File]::WriteAllText(
        $configPath,
        "window.OVERSEER_API_BASE_URL = $configValue;`n",
        [System.Text.UTF8Encoding]::new($false)
    )
    $env:SWA_CLI_DEPLOYMENT_TOKEN = $deploymentToken
    Invoke-SwaDeploy -ContentPath $stagingPath
}
finally {
    if ($null -eq $previousDeploymentToken) {
        Remove-Item Env:\SWA_CLI_DEPLOYMENT_TOKEN -ErrorAction SilentlyContinue
    }
    else {
        $env:SWA_CLI_DEPLOYMENT_TOKEN = $previousDeploymentToken
    }
    if (Test-Path -LiteralPath $stagingPath -PathType Container) {
        Remove-Item -LiteralPath $stagingPath -Recurse -Force
    }
    $deploymentToken = $null
}

Write-Host "Deployment completed."
Write-Host "API: $apiOrigin"
Write-Host "Dashboard: $staticOrigin"
Write-Host "Set FoundryProjectScope to the project ARM resource ID before each deployment."
