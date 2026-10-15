param(
    [switch]$ApplyUpdateOnly
)

$ErrorActionPreference = "Stop"

$ProjectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$UserDataRoot = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "TradeRouter"
} else {
    Join-Path $env:USERPROFILE ".trade-router"
}
$PendingUpdatePath = Join-Path $UserDataRoot "updates\pending-update.json"
$env:TRADE_ROUTER_INSTALL_DIR = $ProjectRoot
Set-Location -LiteralPath $ProjectRoot

function Assert-ChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$Child
    )
    $parentFull = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $childFull = [System.IO.Path]::GetFullPath($Child)
    if (-not $childFull.StartsWith($parentFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Update path is outside the expected directory: $childFull"
    }
    return $childFull
}

function Apply-PendingUpdate {
    if (-not (Test-Path -LiteralPath $PendingUpdatePath)) {
        return
    }

    Write-Host "Applying the downloaded Trade Router update..."
    $pending = Get-Content -Raw -LiteralPath $PendingUpdatePath -Encoding utf8 | ConvertFrom-Json
    $expectedRoot = [System.IO.Path]::GetFullPath([string]$pending.install_root)
    if (-not $expectedRoot.Equals($ProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The update was prepared for a different installation folder."
    }

    $payloadRoot = [System.IO.Path]::GetFullPath([string]$pending.payload_root)
    $updatesRoot = [System.IO.Path]::GetFullPath((Join-Path $UserDataRoot "updates"))
    [void](Assert-ChildPath -Parent $updatesRoot -Child $payloadRoot)
    $manifestPath = Join-Path $payloadRoot "release-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "The downloaded update manifest is missing."
    }
    $manifest = Get-Content -Raw -LiteralPath $manifestPath -Encoding utf8 | ConvertFrom-Json
    if ([string]$manifest.version -ne [string]$pending.version) {
        throw "The downloaded update version does not match the pending update."
    }
    $transactionRoot = Split-Path -Parent $payloadRoot
    $backupRoot = Assert-ChildPath -Parent $updatesRoot -Child (Join-Path $transactionRoot "backup")
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $copiedTargets = [System.Collections.Generic.List[string]]::new()
    $newTargets = [System.Collections.Generic.List[string]]::new()
    try {
        foreach ($entry in $manifest.files) {
            $relative = [string]$entry.path
            if ([string]::IsNullOrWhiteSpace($relative) -or [System.IO.Path]::IsPathRooted($relative)) {
                throw "The update contains an invalid file path."
            }
            $source = Assert-ChildPath -Parent $payloadRoot -Child (Join-Path $payloadRoot $relative)
            $target = Assert-ChildPath -Parent $ProjectRoot -Child (Join-Path $ProjectRoot $relative)
            if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
                throw "An update file is missing: $relative"
            }
            if (Test-Path -LiteralPath $target -PathType Leaf) {
                $backup = Assert-ChildPath -Parent $backupRoot -Child (Join-Path $backupRoot $relative)
                $backupDirectory = Split-Path -Parent $backup
                if (-not (Test-Path -LiteralPath $backupDirectory)) {
                    New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
                }
                Copy-Item -LiteralPath $target -Destination $backup -Force
            } else {
                $newTargets.Add($target)
            }
            $targetDirectory = Split-Path -Parent $target
            if (-not (Test-Path -LiteralPath $targetDirectory)) {
                New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
            }
            Copy-Item -LiteralPath $source -Destination $target -Force
            $copiedTargets.Add($target)
        }
        Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $ProjectRoot "release-manifest.json") -Force
    } catch {
        Write-Host "Update copy failed. Restoring the previous program files..."
        foreach ($backupFile in Get-ChildItem -LiteralPath $backupRoot -Recurse -File) {
            $relative = [System.IO.Path]::GetRelativePath($backupRoot, $backupFile.FullName)
            $target = Assert-ChildPath -Parent $ProjectRoot -Child (Join-Path $ProjectRoot $relative)
            Copy-Item -LiteralPath $backupFile.FullName -Destination $target -Force
        }
        foreach ($target in $newTargets) {
            if (Test-Path -LiteralPath $target -PathType Leaf) {
                Remove-Item -LiteralPath $target -Force
            }
        }
        throw
    }
    Remove-Item -LiteralPath $PendingUpdatePath -Force
    Write-Host "Trade Router v$($pending.version) update applied."
}

function Ensure-ProjectEnvironment {
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    $markerPath = Join-Path $ProjectRoot ".venv\trade-router-project.hash"
    $projectFile = Join-Path $ProjectRoot "pyproject.toml"
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $projectHash = [System.BitConverter]::ToString(
            $sha256.ComputeHash([System.IO.File]::ReadAllBytes($projectFile))
        ).Replace("-", "")
    } finally {
        $sha256.Dispose()
    }

    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host "Preparing the local Python environment..."
        & py -3 -m venv (Join-Path $ProjectRoot ".venv")
        if ($LASTEXITCODE -ne 0) {
            throw "Python 3.11 or later is required."
        }
    }

    $installedHash = if (Test-Path -LiteralPath $markerPath) {
        (Get-Content -Raw -LiteralPath $markerPath).Trim()
    } else {
        ""
    }
    if ($installedHash -ne $projectHash) {
        Write-Host "Installing Trade Router dependencies..."
        & $venvPython -m pip install --disable-pip-version-check -e $ProjectRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Dependency installation failed."
        }
        Set-Content -LiteralPath $markerPath -Value $projectHash -Encoding ascii
    }
    return $venvPython
}

if ($ApplyUpdateOnly) {
    Apply-PendingUpdate
    exit 0
}

$skipBrowser = $false
while ($true) {
    Apply-PendingUpdate
    $venvPython = Ensure-ProjectEnvironment
    Write-Host "Trade Router: http://127.0.0.1:8890"
    Write-Host "Close this window to stop Trade Router."
    if ($skipBrowser) {
        & $venvPython -m trade_router_studio --no-browser
    } else {
        & $venvPython -m trade_router_studio
    }
    $routerExitCode = $LASTEXITCODE
    if ($routerExitCode -eq 75) {
        Write-Host "Restarting Trade Router..."
        $skipBrowser = $true
        continue
    }
    if ($routerExitCode -eq 76) {
        $skipBrowser = $true
        continue
    }
    exit $routerExitCode
}
