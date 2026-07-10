<#
.SYNOPSIS
    Enables and starts the Linien server on one or more Red Pitaya devices.

.DESCRIPTION
    Uses the built-in Windows OpenSSH client (ssh.exe). No additional software
    or PowerShell modules are required.

    The default Red Pitaya username is root. ssh.exe prompts for the password
    separately for each device; the factory-default password is root.

    For each device, the script:
      1. Runs "linien-server enable" to configure startup on boot.
      2. Runs "linien-server start" to start the server immediately.
      3. Verifies that linien-server.service is enabled and active.

.EXAMPLE
    .\scripts\Enable-LinienAutostart.ps1 -Hosts 192.168.1.101,192.168.1.102

.EXAMPLE
    .\scripts\Enable-LinienAutostart.ps1 -HostFile .\red-pitayas.txt

    The host file should contain one IP address or hostname per line. Blank
    lines and lines beginning with # are ignored.
#>

[CmdletBinding(DefaultParameterSetName = "Hosts")]
param(
    [Parameter(Mandatory = $true, ParameterSetName = "Hosts")]
    [string[]]$Hosts,

    [Parameter(Mandatory = $true, ParameterSetName = "File")]
    [ValidateScript({ Test-Path $_ -PathType Leaf })]
    [string]$HostFile,

    [string]$User = "root",

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

$remoteCommand = @'
set -e

if ! command -v linien-server >/dev/null 2>&1; then
    echo "ERROR: linien-server is not installed or is not in PATH." >&2
    exit 10
fi

linien-server enable
linien-server start

enabled="$(systemctl is-enabled linien-server.service 2>/dev/null || true)"
active="$(systemctl is-active linien-server.service 2>/dev/null || true)"

echo "enabled=$enabled"
echo "active=$active"

if [ "$enabled" != "enabled" ] || [ "$active" != "active" ]; then
    echo "ERROR: Linien service verification failed." >&2
    systemctl status linien-server.service --no-pager --full || true
    exit 11
fi
'@

$results = foreach ($target in $targets) {
    Write-Host "`n[$target] Connecting as $User..." -ForegroundColor Cyan
    Write-Host "Enter the Red Pitaya password when prompted (factory default: root)." -ForegroundColor DarkGray

    $sshArgs = @(
        "-p", $Port
        "-o", "ConnectTimeout=$ConnectTimeoutSeconds"
        "-o", "StrictHostKeyChecking=accept-new"
        "$User@$target"
        $remoteCommand
    )

    # Temporarily avoid treating native stderr output, including the SSH
    # password prompt, as a terminating PowerShell error.
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $ssh.Source @sshArgs 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($output) {
        $output | ForEach-Object { Write-Host "  $_" }
    }

    if ($exitCode -eq 0) {
        Write-Host "[$target] Linien is enabled for boot and is running now." -ForegroundColor Green
        $status = "Success"
    }
    else {
        Write-Host "[$target] Failed with exit code $exitCode." -ForegroundColor Red
        $status = "Failed"
    }

    [pscustomobject]@{
        Host     = $target
        Result   = $status
        ExitCode = $exitCode
    }
}

Write-Host "`nSummary" -ForegroundColor Cyan
$results | Format-Table Host, Result, ExitCode -AutoSize

if ($results.Result -contains "Failed") {
    exit 1
}

exit 0
