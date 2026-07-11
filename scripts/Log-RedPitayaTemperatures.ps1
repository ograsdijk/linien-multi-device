<#
.SYNOPSIS
    Periodically logs Red Pitaya temperatures to a local CSV file.

.DESCRIPTION
    Uses the built-in Windows OpenSSH client (ssh.exe). No additional software
    or PowerShell modules are required.

    The script accepts either:
      - A list of IP addresses or hostnames with -Hosts
      - A text file containing one address per line with -HostFile

    By default, it polls every 60 seconds and runs continuously until stopped
    with Ctrl+C. Use -IntervalSeconds to change the polling interval or -Once
    to collect a single set of readings.

    Passwordless SSH keys are recommended. By default, BatchMode is enabled so
    an unavailable SSH key causes that reading to fail rather than leaving the
    logger waiting at a password prompt. Use -AllowPasswordPrompt to permit
    interactive password prompts.

.EXAMPLE
    .\Log-RedPitayaTemperatures.ps1 `
        -Hosts 192.168.1.2,192.168.1.3 `
        -IntervalSeconds 30

.EXAMPLE
    .\Log-RedPitayaTemperatures.ps1 `
        -HostFile .\red-pitayas.txt `
        -IntervalSeconds 300 `
        -OutputCsv .\logs\red-pitaya-temperatures.csv

.EXAMPLE
    .\Log-RedPitayaTemperatures.ps1 `
        -Hosts 192.168.1.2,192.168.1.3 `
        -Once

.NOTES
    CSV columns:
      TimestampLocal, TimestampUtc, Host, TemperatureC, Status, Error
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

    [ValidateRange(1, 86400)]
    [double]$IntervalSeconds = 60,

    [string]$OutputCsv = ".\red-pitaya-temperatures.csv",

    [ValidateRange(1, 300)]
    [int]$ConnectTimeoutSeconds = 10,

    [string]$IdentityFile,

    [switch]$Once,

    [switch]$AllowPasswordPrompt
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

if ($IdentityFile) {
    $IdentityFile = [System.IO.Path]::GetFullPath($IdentityFile)

    if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) {
        throw "SSH identity file not found: $IdentityFile"
    }
}

$OutputCsv = [System.IO.Path]::GetFullPath($OutputCsv)
$outputDirectory = Split-Path -Parent $OutputCsv

if (-not (Test-Path -LiteralPath $outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

# Red Pitaya's XADC temperature is exposed through the Linux IIO sysfs
# interface. The exact iio:device number can vary, so discover it by looking
# for the required temperature files.
$remoteCommand = @'
set -e

sensor_dir=""
fallback_dir=""
candidate_count=0

for candidate in /sys/bus/iio/devices/iio:device*; do
    if [ -r "$candidate/in_temp0_raw" ] &&
       [ -r "$candidate/in_temp0_scale" ]; then
        candidate_count=$((candidate_count + 1))
        fallback_dir="$candidate"

        if [ -r "$candidate/name" ] &&
           grep -qi 'xadc' "$candidate/name"; then
            sensor_dir="$candidate"
            break
        fi
    fi
done

# Older Red Pitaya images may expose no useful IIO name. Falling back is safe
# only when exactly one device provides temperature attributes.
if [ -z "$sensor_dir" ] && [ "$candidate_count" -eq 1 ]; then
    sensor_dir="$fallback_dir"
fi

if [ -z "$sensor_dir" ]; then
    echo "No unambiguous readable XADC temperature sensor was found." >&2
    exit 20
fi

raw="$(cat "$sensor_dir/in_temp0_raw")"
scale="$(cat "$sensor_dir/in_temp0_scale")"
offset="0"

if [ -r "$sensor_dir/in_temp0_offset" ]; then
    offset="$(cat "$sensor_dir/in_temp0_offset")"
fi

awk -v raw="$raw" -v scale="$scale" -v offset="$offset" \
    'BEGIN { printf "%.3f\n", ((raw + offset) * scale) / 1000.0 }'
'@

Write-Host "Red Pitaya temperature logger" -ForegroundColor Cyan
Write-Host "  Hosts:    $($targets -join ', ')"
Write-Host "  Interval: $IntervalSeconds seconds"
Write-Host "  CSV:      $OutputCsv"

if ($Once) {
    Write-Host "  Mode:     one reading per host"
}
else {
    Write-Host "  Mode:     continuous; press Ctrl+C to stop"
}

if (-not $AllowPasswordPrompt) {
    Write-Host "  SSH:      key authentication required (BatchMode=yes)"
}
else {
    Write-Host "  SSH:      password prompts allowed"
}

$cycleNumber = 0

while ($true) {
    $cycleNumber++
    $cycleStarted = Get-Date

    Write-Host "`nCycle $cycleNumber - $($cycleStarted.ToString('yyyy-MM-dd HH:mm:ss'))" `
        -ForegroundColor Cyan

    foreach ($target in $targets) {
        $sampleTime = Get-Date
        $timestampLocal = $sampleTime.ToString("yyyy-MM-ddTHH:mm:ss.fffK")
        $timestampUtc = $sampleTime.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")

        $sshArgs = @(
            "-p", $Port
            "-o", "ConnectTimeout=$ConnectTimeoutSeconds"
            "-o", "ServerAliveInterval=5"
            "-o", "ServerAliveCountMax=1"
            "-o", "StrictHostKeyChecking=accept-new"
        )

        if (-not $AllowPasswordPrompt) {
            $sshArgs += @("-o", "BatchMode=yes")
        }

        if ($IdentityFile) {
            $sshArgs += @(
                "-i", $IdentityFile
                "-o", "IdentitiesOnly=yes"
            )
        }

        $sshArgs += "$User@$target"
        $sshArgs += $remoteCommand

        try {
            # Avoid PowerShell converting normal ssh.exe stderr output into a
            # terminating NativeCommandError. The SSH exit code is checked below.
            $previousErrorActionPreference = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            try {
                $output = @(& $ssh.Source @sshArgs 2>&1)
                $exitCode = $LASTEXITCODE
            }
            finally {
                $ErrorActionPreference = $previousErrorActionPreference
            }

            $temperatureLine = $output |
                ForEach-Object { "$_".Trim() } |
                Where-Object {
                    $_ -match '^-?[0-9]+(?:\.[0-9]+)?$'
                } |
                Select-Object -Last 1

            if ($exitCode -eq 0 -and $null -ne $temperatureLine) {
                $temperature = [double]::Parse(
                    $temperatureLine,
                    [System.Globalization.CultureInfo]::InvariantCulture
                )

                $status = "Success"
                $errorText = ""

                Write-Host (
                    "[{0}] {1:N3} C" -f $target, $temperature
                ) -ForegroundColor Green
            }
            else {
                $temperature = $null
                $status = "Failed"
                $errorText = ($output -join " | ").Trim()

                if (-not $errorText) {
                    $errorText = "ssh.exe exited with code $exitCode without a temperature value."
                }

                Write-Host "[$target] Failed: $errorText" -ForegroundColor Red
            }
        }
        catch {
            $temperature = $null
            $status = "Failed"
            $errorText = $_.Exception.Message

            Write-Host "[$target] Failed: $errorText" -ForegroundColor Red
        }

        $row = [pscustomobject][ordered]@{
            TimestampLocal = $timestampLocal
            TimestampUtc   = $timestampUtc
            Host           = $target
            TemperatureC   = $temperature
            Status         = $status
            Error          = $errorText
        }

        $row | Export-Csv `
            -LiteralPath $OutputCsv `
            -NoTypeInformation `
            -Append `
            -Encoding utf8
    }

    if ($Once) {
        break
    }

    # Keep cycle starts approximately IntervalSeconds apart. If polling all
    # hosts takes longer than the requested interval, begin the next cycle
    # immediately and print a warning.
    $elapsedSeconds = ((Get-Date) - $cycleStarted).TotalSeconds
    $sleepSeconds = $IntervalSeconds - $elapsedSeconds

    if ($sleepSeconds -gt 0) {
        Write-Host (
            "Next cycle in {0:N1} seconds." -f $sleepSeconds
        ) -ForegroundColor DarkGray

        Start-Sleep -Milliseconds ([int][Math]::Ceiling($sleepSeconds * 1000))
    }
    else {
        $warningMessage = (
            "Polling took {0:N1} seconds, longer than the {1:N1}-second interval; " +
            "starting the next cycle immediately."
        ) -f $elapsedSeconds, $IntervalSeconds

        Write-Host $warningMessage -ForegroundColor Yellow
    }
}

Write-Host "`nFinished. CSV written to:" -ForegroundColor Cyan
Write-Host "  $OutputCsv"
