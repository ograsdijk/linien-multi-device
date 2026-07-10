<#
.SYNOPSIS
    Installs a local Windows OpenSSH public key on one or more Red Pitaya devices.

.DESCRIPTION
    Uses only the built-in Windows OpenSSH tools (ssh.exe and ssh-keygen.exe).
    No additional software or PowerShell modules are required.

    Accepts either -Hosts or -HostFile. Creates a passwordless ED25519 key if
    needed, installs it idempotently, and verifies passwordless login.

.EXAMPLE
    .\scripts\Install-RedPitayaSshKeys.ps1 -Hosts 192.168.1.2,192.168.1.3

.EXAMPLE
    .\scripts\Install-RedPitayaSshKeys.ps1 -HostFile .\red-pitayas.txt
#>

[CmdletBinding(DefaultParameterSetName = "Hosts")]
param(
    [Parameter(Mandatory = $true, ParameterSetName = "Hosts")]
    [string[]]$Hosts,

    [Parameter(Mandatory = $true, ParameterSetName = "File")]
    [ValidateScript({ Test-Path $_ -PathType Leaf })]
    [string]$HostFile,

    [string]$User = "root",
    [string]$KeyPath = "$env:USERPROFILE\.ssh\id_ed25519",

    [ValidateRange(1, 65535)]
    [int]$Port = 22,

    [ValidateRange(1, 300)]
    [int]$ConnectTimeoutSeconds = 10
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ssh = Get-Command ssh.exe -ErrorAction SilentlyContinue
if (-not $ssh) {
    throw "ssh.exe was not found. Enable the built-in Windows OpenSSH Client feature."
}

$sshKeygen = Get-Command ssh-keygen.exe -ErrorAction SilentlyContinue
if (-not $sshKeygen) {
    throw "ssh-keygen.exe was not found. Enable the built-in Windows OpenSSH Client feature."
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList
    )

    $oldErrorActionPreference = $ErrorActionPreference
    $hasNativePreference = Test-Path variable:PSNativeCommandUseErrorActionPreference
    if ($hasNativePreference) {
        $oldNativePreference = $PSNativeCommandUseErrorActionPreference
    }

    try {
        $ErrorActionPreference = "Continue"
        if ($hasNativePreference) {
            $PSNativeCommandUseErrorActionPreference = $false
        }

        $output = @(& $FilePath @ArgumentList 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldErrorActionPreference
        if ($hasNativePreference) {
            $PSNativeCommandUseErrorActionPreference = $oldNativePreference
        }
    }

    [pscustomobject]@{
        Output   = $output
        ExitCode = $exitCode
    }
}

if ($PSCmdlet.ParameterSetName -eq "File") {
    $targets = @(
        Get-Content -LiteralPath $HostFile |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -and -not $_.StartsWith("#") } |
            Sort-Object -Unique
    )
}
else {
    $targets = @(
        $Hosts |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ } |
            Sort-Object -Unique
    )
}

if ($targets.Count -eq 0) {
    throw "No valid hostnames or IP addresses were provided."
}

$KeyPath = [System.IO.Path]::GetFullPath($KeyPath)
$publicKeyPath = "$KeyPath.pub"
$keyDirectory = Split-Path -Parent $KeyPath

if (-not (Test-Path -LiteralPath $keyDirectory)) {
    New-Item -ItemType Directory -Path $keyDirectory -Force | Out-Null
}

if (-not (Test-Path -LiteralPath $KeyPath)) {
    Write-Host "Creating passwordless ED25519 key:" -ForegroundColor Cyan
    Write-Host "  $KeyPath"

    # PowerShell 5.1 needs the quoted empty string to preserve the empty
    # passphrase argument when calling a native executable.
    $keygenResult = Invoke-NativeCommand `
        -FilePath $sshKeygen.Source `
        -ArgumentList @("-q", "-t", "ed25519", "-f", $KeyPath, "-N", '""')

    if ($keygenResult.ExitCode -ne 0) {
        $details = ($keygenResult.Output -join [Environment]::NewLine)
        throw "ssh-keygen failed with exit code $($keygenResult.ExitCode).`n$details"
    }
}
else {
    Write-Host "Using existing SSH key:" -ForegroundColor Cyan
    Write-Host "  $KeyPath"
}

if (-not (Test-Path -LiteralPath $publicKeyPath)) {
    Write-Host "Public key file is missing; recreating it from the private key..." -ForegroundColor Yellow

    $deriveResult = Invoke-NativeCommand `
        -FilePath $sshKeygen.Source `
        -ArgumentList @("-y", "-f", $KeyPath)

    $derivedPublicKey = ($deriveResult.Output -join "`n").Trim()
    if ($deriveResult.ExitCode -ne 0 -or -not $derivedPublicKey) {
        throw "Could not derive the public key from $KeyPath."
    }

    Set-Content -LiteralPath $publicKeyPath -Value $derivedPublicKey -Encoding ascii
}

$publicKey = (Get-Content -LiteralPath $publicKeyPath -Raw).Trim()
if (-not $publicKey) {
    throw "The public key file is empty: $publicKeyPath"
}

# Base64 contains only shell-safe characters. Passing the key this way avoids
# piping Windows CRLF data into SSH and avoids multiline Bash command parsing.
$publicKeyBase64 = [Convert]::ToBase64String(
    [System.Text.Encoding]::UTF8.GetBytes($publicKey)
)

$results = foreach ($target in $targets) {
    Write-Host "`n[$target] Installing key for $User..." -ForegroundColor Cyan
    Write-Host "Enter the Red Pitaya password if prompted (factory default: root)." -ForegroundColor DarkGray

    $remoteInstallCommand = "umask 077; mkdir -p `"`$HOME/.ssh`"; touch `"`$HOME/.ssh/authorized_keys`"; key=`"`$(printf '%s' '$publicKeyBase64' | base64 -d)`"; if grep -qxF `"`$key`" `"`$HOME/.ssh/authorized_keys`"; then echo key=already_present; else printf '%s\n' `"`$key`" >> `"`$HOME/.ssh/authorized_keys`"; echo key=installed; fi; chmod 700 `"`$HOME/.ssh`"; chmod 600 `"`$HOME/.ssh/authorized_keys`""

    $installArgs = @(
        "-p", "$Port",
        "-o", "ConnectTimeout=$ConnectTimeoutSeconds",
        "-o", "StrictHostKeyChecking=accept-new",
        "$User@$target",
        $remoteInstallCommand
    )

    $installResult = Invoke-NativeCommand `
        -FilePath $ssh.Source `
        -ArgumentList $installArgs

    if ($installResult.Output) {
        $installResult.Output | ForEach-Object { Write-Host "  $_" }
    }

    if ($installResult.ExitCode -ne 0) {
        Write-Host "[$target] Key installation failed with exit code $($installResult.ExitCode)." -ForegroundColor Red

        [pscustomobject]@{
            Host         = $target
            Install      = "Failed"
            Verification = "Not run"
            ExitCode     = $installResult.ExitCode
        }
        continue
    }

    Write-Host "[$target] Verifying passwordless login..." -ForegroundColor Cyan

    $verifyArgs = @(
        "-p", "$Port",
        "-i", $KeyPath,
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=$ConnectTimeoutSeconds",
        "-o", "StrictHostKeyChecking=accept-new",
        "$User@$target",
        "printf 'SSH_KEY_OK\n'"
    )

    $verifyResult = Invoke-NativeCommand `
        -FilePath $ssh.Source `
        -ArgumentList $verifyArgs

    if ($verifyResult.Output) {
        $verifyResult.Output | ForEach-Object { Write-Host "  $_" }
    }

    if ($verifyResult.ExitCode -eq 0 -and ($verifyResult.Output -join "`n") -match "SSH_KEY_OK") {
        Write-Host "[$target] Passwordless SSH verified." -ForegroundColor Green
        $verification = "Success"
        $overallExitCode = 0
    }
    else {
        Write-Host "[$target] Key was installed, but passwordless verification failed." -ForegroundColor Red
        $verification = "Failed"
        $overallExitCode = $verifyResult.ExitCode
    }

    [pscustomobject]@{
        Host         = $target
        Install      = "Success"
        Verification = $verification
        ExitCode     = $overallExitCode
    }
}

Write-Host "`nSummary" -ForegroundColor Cyan
$results | Format-Table Host, Install, Verification, ExitCode -AutoSize

if (($results.Install -contains "Failed") -or ($results.Verification -contains "Failed")) {
    exit 1
}

exit 0
