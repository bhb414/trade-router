param(
    [switch]$ApplyUpdateOnly,
    [switch]$CheckPythonOnly,
    [switch]$PrepareEnvironmentOnly
)

$ErrorActionPreference = "Stop"

$ProjectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$UserDataRoot = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "TradeRouter"
} else {
    Join-Path $env:USERPROFILE ".trade-router"
}
$InstallUpdatesRoot = Join-Path $ProjectRoot ".trade-router-updates"
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

function Find-PendingUpdatePath {
    $candidates = [System.Collections.Generic.List[string]]::new()
    $candidates.Add((Join-Path $InstallUpdatesRoot "pending-update.json"))
    $candidates.Add((Join-Path $UserDataRoot "updates\pending-update.json"))

    # Microsoft Store Python can redirect LocalAppData into this package
    # LocalCache. v0.1.5 and earlier staged updates there while this launcher
    # looked only at the ordinary LocalAppData path.
    if ($env:LOCALAPPDATA) {
        $storePattern = Join-Path $env:LOCALAPPDATA (
            "Packages\PythonSoftwareFoundation.Python.*" +
            "\LocalCache\Local\TradeRouter\updates\pending-update.json"
        )
        foreach ($item in Get-Item -Path $storePattern -ErrorAction SilentlyContinue |
            Sort-Object -Property LastWriteTime -Descending) {
            $candidates.Add($item.FullName)
        }
    }

    $installedVersion = $null
    $installedManifestPath = Join-Path $ProjectRoot "release-manifest.json"
    if (Test-Path -LiteralPath $installedManifestPath -PathType Leaf) {
        try {
            $installedManifest = Get-Content -Raw -LiteralPath $installedManifestPath -Encoding utf8 |
                ConvertFrom-Json
            $installedVersion = [version]([string]$installedManifest.version)
        } catch {
            $installedVersion = $null
        }
    }

    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        $candidateFull = [System.IO.Path]::GetFullPath($candidate)
        try {
            $candidatePending = Get-Content -Raw -LiteralPath $candidateFull -Encoding utf8 |
                ConvertFrom-Json
            $candidateInstallRoot = [System.IO.Path]::GetFullPath(
                [string]$candidatePending.install_root
            )
        } catch {
            if ($candidateFull.StartsWith(
                ([System.IO.Path]::GetFullPath($InstallUpdatesRoot).TrimEnd('\') + '\'),
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                return $candidateFull
            }
            continue
        }
        if (-not $candidateInstallRoot.Equals(
            $ProjectRoot,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            continue
        }
        if ($null -ne $installedVersion) {
            try {
                $pendingVersion = [version]([string]$candidatePending.version)
                if ($pendingVersion -le $installedVersion) {
                    Remove-Item -LiteralPath $candidateFull -Force
                    continue
                }
            } catch {
                return $candidateFull
            }
        }
        return $candidateFull
    }
    return $null
}

function Apply-PendingUpdate {
    $pendingUpdatePath = Find-PendingUpdatePath
    if ([string]::IsNullOrWhiteSpace($pendingUpdatePath)) {
        return
    }

    Write-Host "Applying the downloaded Trade Router update..."
    $pending = Get-Content -Raw -LiteralPath $pendingUpdatePath -Encoding utf8 | ConvertFrom-Json
    $expectedRoot = [System.IO.Path]::GetFullPath([string]$pending.install_root)
    if (-not $expectedRoot.Equals($ProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The update was prepared for a different installation folder."
    }

    $payloadRoot = [System.IO.Path]::GetFullPath([string]$pending.payload_root)
    $updatesRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $pendingUpdatePath))
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
    Remove-Item -LiteralPath $pendingUpdatePath -Force
    Write-Host "Trade Router v$($pending.version) update applied."
}

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$PrefixArguments = @()
    )
    try {
        $probeArguments = @($PrefixArguments) + @(
            "-c",
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
        )
        & $FilePath @probeArguments *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Find-CompatiblePython {
    $candidates = [System.Collections.Generic.List[object]]::new()
    if (-not [string]::IsNullOrWhiteSpace($env:TRADE_ROUTER_PYTHON)) {
        $candidates.Add([pscustomobject]@{
            FilePath = $env:TRADE_ROUTER_PYTHON
            PrefixArguments = @()
            DisplayName = $env:TRADE_ROUTER_PYTHON
        })
    }

    foreach ($candidate in @(
        @{ Name = "python"; PrefixArguments = @() },
        @{ Name = "python3"; PrefixArguments = @() },
        @{ Name = "python3.13"; PrefixArguments = @() },
        @{ Name = "python3.12"; PrefixArguments = @() },
        @{ Name = "python3.11"; PrefixArguments = @() },
        @{ Name = "py"; PrefixArguments = @("-3") }
    )) {
        $command = Get-Command $candidate.Name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $command) {
            continue
        }
        $displayName = if ($candidate.PrefixArguments.Count -gt 0) {
            "$($candidate.Name) $($candidate.PrefixArguments -join ' ')"
        } else {
            $candidate.Name
        }
        $candidates.Add([pscustomobject]@{
            FilePath = $command.Source
            PrefixArguments = @($candidate.PrefixArguments)
            DisplayName = $displayName
        })
    }

    foreach ($registryRoot in @(
        "HKCU:\Software\Python\PythonCore",
        "HKLM:\Software\Python\PythonCore",
        "HKLM:\Software\WOW6432Node\Python\PythonCore"
    )) {
        if (-not (Test-Path -LiteralPath $registryRoot)) {
            continue
        }
        try {
            $versions = Get-ChildItem -LiteralPath $registryRoot -ErrorAction Stop |
                Sort-Object -Property PSChildName -Descending
            foreach ($version in $versions) {
                $installKeyPath = Join-Path $version.PSPath "InstallPath"
                if (-not (Test-Path -LiteralPath $installKeyPath)) {
                    continue
                }
                $installKey = Get-Item -LiteralPath $installKeyPath -ErrorAction Stop
                $executablePath = [string]$installKey.GetValue("ExecutablePath", "")
                if ([string]::IsNullOrWhiteSpace($executablePath)) {
                    $installDirectory = [string]$installKey.GetValue("", "")
                    if (-not [string]::IsNullOrWhiteSpace($installDirectory)) {
                        $executablePath = Join-Path $installDirectory "python.exe"
                    }
                }
                if (Test-Path -LiteralPath $executablePath -PathType Leaf) {
                    $candidates.Add([pscustomobject]@{
                        FilePath = $executablePath
                        PrefixArguments = @()
                        DisplayName = "Python $($version.PSChildName)"
                    })
                }
            }
        } catch {
            # A protected or incomplete registry entry must not block other candidates.
        }
    }

    $pythonPatterns = [System.Collections.Generic.List[string]]::new()
    if ($env:LOCALAPPDATA) {
        $pythonPatterns.Add((Join-Path $env:LOCALAPPDATA "Programs\Python\Python*\python.exe"))
    }
    if ($env:ProgramFiles) {
        $pythonPatterns.Add((Join-Path $env:ProgramFiles "Python*\python.exe"))
    }
    if (${env:ProgramFiles(x86)}) {
        $pythonPatterns.Add((Join-Path ${env:ProgramFiles(x86)} "Python*\python.exe"))
    }
    if ($env:USERPROFILE) {
        $pythonPatterns.Add((Join-Path $env:USERPROFILE "scoop\apps\python\current\python.exe"))
        $pythonPatterns.Add((Join-Path $env:USERPROFILE "miniconda3\python.exe"))
        $pythonPatterns.Add((Join-Path $env:USERPROFILE "anaconda3\python.exe"))
    }
    foreach ($pattern in $pythonPatterns) {
        foreach ($pythonFile in @(Get-ChildItem -Path $pattern -File -ErrorAction SilentlyContinue |
            Sort-Object -Property FullName -Descending)) {
            $candidates.Add([pscustomobject]@{
                FilePath = $pythonFile.FullName
                PrefixArguments = @()
                DisplayName = $pythonFile.FullName
            })
        }
    }

    foreach ($candidate in $candidates) {
        if (Test-PythonCandidate `
            -FilePath $candidate.FilePath `
            -PrefixArguments $candidate.PrefixArguments) {
            return $candidate
        }
    }
    return $null
}

function Get-PythonRequirementMessage {
    return @"
Python 3.11 or later was not found.
Install Python from https://www.python.org/downloads/windows/ and enable
"Add python.exe to PATH" during setup, then close this window and run run.cmd again.
"@
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
        $python = Find-CompatiblePython
        if ($null -eq $python) {
            throw (Get-PythonRequirementMessage)
        }
        Write-Host "Using $($python.DisplayName) to create the local environment."
        $venvArguments = @($python.PrefixArguments) + @(
            "-m",
            "venv",
            (Join-Path $ProjectRoot ".venv")
        )
        & $python.FilePath @venvArguments 2>&1 | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) {
            throw "Python was found, but the local environment could not be created."
        }
    }

    $installedHash = if (Test-Path -LiteralPath $markerPath) {
        (Get-Content -Raw -LiteralPath $markerPath).Trim()
    } else {
        ""
    }
    if ($installedHash -ne $projectHash) {
        Write-Host "Installing Trade Router dependencies..."
        & $venvPython -m pip install --disable-pip-version-check -e $ProjectRoot 2>&1 |
            ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) {
            throw "Dependency installation failed."
        }
        Set-Content -LiteralPath $markerPath -Value $projectHash -Encoding ascii
    }
    return $venvPython
}

try {
    if ($CheckPythonOnly) {
        $python = Find-CompatiblePython
        if ($null -eq $python) {
            throw (Get-PythonRequirementMessage)
        }
        Write-Output $python.DisplayName
        exit 0
    }

    if ($ApplyUpdateOnly) {
        Apply-PendingUpdate
        exit 0
    }

    if ($PrepareEnvironmentOnly) {
        $venvPython = Ensure-ProjectEnvironment
        if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
            throw "The local Python environment was not prepared correctly."
        }
        Write-Output $venvPython
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
} catch {
    Write-Host ""
    Write-Host "Trade Router could not start." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
