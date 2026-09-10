#Requires -Version 5.1
<#
.SYNOPSIS
  Create or attach a RunPod network volume + GPU pod for 4DAnyone (H100 80GB, 150GB volume, 8000/http exposed).

.DESCRIPTION
  Native PowerShell port of scripts/deploy.sh. Idempotent on volume (reuses
  by name+DC); refuses to create a duplicate pod. No bash, no jq.

  PS 5.1 has a known bug where ConvertFrom-Json on the runpodctl datacenter
  list collapses nested arrays. We use `-AsHashtable` on PS 6+ and fall back
  to `JavaScriptSerializer` only on PS 5.1.

.PARAMETER PodName / VolumeName / VolumeSizeGB / GpuId / GpuCount / PodImage / ContainerDiskGB / DataCenter / CloudType
  Override defaults. Each also accepts an env-var fallback (POD_NAME, etc.).

.NOTES
  Required env vars (set in YOUR shell, never in this file):
    RUNPOD_API_KEY   authenticates runpodctl, OR run: runpodctl doctor (cached)
    HF_TOKEN         OPTIONAL — only needed if your HF model is gated, or
                     you want pre-baked auth for hf downloads on the pod

.EXAMPLE
  $env:HF_TOKEN = "<paste>"
  .\scripts\deploy.ps1
#>

[CmdletBinding()]
param(
    [string] $PodName        = $(if ($env:POD_NAME)        { $env:POD_NAME }        else { 'fdanyone' }),
    [string] $VolumeName     = $(if ($env:VOLUME_NAME)     { $env:VOLUME_NAME }     else { 'fdanyone-workspace' }),
    [int]    $VolumeSizeGB   = $(if ($env:VOLUME_SIZE_GB)  { [int]$env:VOLUME_SIZE_GB }  else { 150 }),
    [string] $GpuId          = $(if ($env:GPU_ID)          { $env:GPU_ID }          else { 'NVIDIA H100 80GB HBM3' }),
    [int]    $GpuCount       = $(if ($env:GPU_COUNT)       { [int]$env:GPU_COUNT }  else { 1 }),
    # Tag schema: 1.0.X-cuXXXX-torchYYY-ubuntuYYYY (ALL FOUR fields required).
    # Confirm exact tag exists on Docker Hub before deploy:
    #   curl -s 'https://hub.docker.com/v2/repositories/runpod/pytorch/tags?page_size=50&ordering=last_updated' | jq '.results[].name'
    # Bad tag = pod sits in container-create loop forever ("manifest unknown").
    [string] $PodImage       = $(if ($env:POD_IMAGE)       { $env:POD_IMAGE }       else { 'runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404' }),
    [int]    $ContainerDiskGB= $(if ($env:CONTAINER_DISK_GB){ [int]$env:CONTAINER_DISK_GB } else { 60 }),
    [string] $DataCenter     = $(if ($env:DATA_CENTER_ID)  { $env:DATA_CENTER_ID }  else { 'auto' }),
    [ValidateSet('SECURE','COMMUNITY')]
    [string] $CloudType      = $(if ($env:CLOUD_TYPE)      { $env:CLOUD_TYPE }      else { 'SECURE' })
)

# IMPORTANT: 'Continue' so native commands (runpodctl) writing to stderr
# don't terminate the script. We capture stderr via 2>&1 and inspect ourselves.
$ErrorActionPreference = 'Continue'

# In PS 7.3+, native command errors are wrapped as PowerShell errors that
# respect $ErrorActionPreference unless this is set to $false.
try { $PSNativeCommandUseErrorActionPreference = $false } catch {}

# Force consistent native-arg passing across PS 5.1 and 7+.
try { $PSNativeCommandArgumentPassing = 'Legacy' } catch {}

function Log  { param($Msg) Write-Host "[deploy] $Msg" -ForegroundColor Cyan }
function Warn { param($Msg) Write-Host "[deploy] $Msg" -ForegroundColor Yellow }
function Die  { param($Msg) Write-Host "[deploy] $Msg" -ForegroundColor Red; exit 1 }

# JSON parsing helper: PS 6+ uses -AsHashtable (clean, indexable); PS 5.1
# falls back to JavaScriptSerializer (avoids the nested-array wrapping bug).
function ConvertFrom-RunpodJson {
    param([string] $Json)
    if (-not $Json) { return @() }
    if ($PSVersionTable.PSVersion.Major -ge 6) {
        try {
            $parsed = $Json | ConvertFrom-Json -Depth 100 -AsHashtable
            if ($parsed -is [System.Collections.IList]) { return @($parsed) }
            elseif ($parsed) { return @($parsed) }
            return @()
        } catch {
            Warn "ConvertFrom-Json failed: $($_.Exception.Message)"
            return @()
        }
    } else {
        try {
            Add-Type -AssemblyName System.Web.Extensions -ErrorAction Stop
            $jss = New-Object System.Web.Script.Serialization.JavaScriptSerializer
            $jss.MaxJsonLength = 256MB
            $parsed = $jss.DeserializeObject($Json)
            if ($parsed -is [System.Collections.IList]) { return @($parsed) }
            elseif ($parsed) { return @($parsed) }
            return @()
        } catch {
            Warn "JavaScriptSerializer failed: $($_.Exception.Message)"
            return @()
        }
    }
}

# ─────────────────────────────────────────────────────────────
# Pre-flight
# ─────────────────────────────────────────────────────────────
if (-not (Get-Command runpodctl -ErrorAction SilentlyContinue)) {
    Die "runpodctl not on PATH. Verify with: where.exe runpodctl"
}
Log "Using runpodctl: $((Get-Command runpodctl).Source)"

if (-not $env:HF_TOKEN) {
    Warn 'HF_TOKEN not set. Continuing — anonymous HF downloads work for public models.'
    Warn '  If you hit a gated model later, run: $env:HF_TOKEN = "<paste>" then redeploy.'
}

# Auth: env var OR cached config (~/.runpod/config.toml)
if (-not $env:RUNPOD_API_KEY) {
    $userOut = (runpodctl user 2>&1) | Out-String
    if ($userOut -match '"error"') {
        Warn "runpodctl user returned an error:"
        ($userOut -split "`n") | ForEach-Object { Write-Host "  | $_" }
        Die "Auth failed. Run: runpodctl doctor (paste your API key when prompted)."
    }
    try {
        $user = $userOut | ConvertFrom-Json
        if (-not ($user.id -or $user.email -or $user.userId)) {
            Warn "runpodctl user output did not include id/email:"
            ($userOut -split "`n") | ForEach-Object { Write-Host "  | $_" }
            Die "Auth check failed."
        }
        # Pre-compute interpolated values to avoid `\$$(...)` interpolation
        # gotchas on PS 7 (the literal `\$` outside a string vs `$()` parses
        # ambiguously and can render the whole expression as text).
        $balance = [math]::Round([double]$user.clientBalance, 2)
        Log ("Account: {0} (balance `${1}, spend limit `${2})" -f $user.email, $balance, $user.spendLimit)
    } catch {
        Warn "Could not parse runpodctl user output as JSON:"
        ($userOut -split "`n") | ForEach-Object { Write-Host "  | $_" }
        Die "Auth check failed."
    }
}

# ─────────────────────────────────────────────────────────────
# Network volume — reuse or create. Volumes are pinned to their datacenter,
# so if a volume named $VolumeName exists in DC X, we either run the pod
# in DC X or create a fresh volume in a different DC.
# ─────────────────────────────────────────────────────────────
Log "Looking for existing volume named '$VolumeName'"
$volumeRaw = (runpodctl network-volume list -o json 2>$null) | Out-String
$volumeList = @()
try { $volumeList = @($volumeRaw | ConvertFrom-Json) } catch { $volumeList = @() }

function Get-VolumeDc {
    param($Volume)
    $dc = $Volume.dataCenterId
    if (-not $dc) { $dc = $Volume.dataCenter }
    if (-not $dc) { $dc = $Volume.location }
    [string]$dc
}

$matchingVolumes = @($volumeList | Where-Object { $_.name -eq $VolumeName })
$existingByDc = @{}
foreach ($v in $matchingVolumes) {
    $dc = Get-VolumeDc -Volume $v
    if ($dc) { $existingByDc[$dc] = $v }
}

if ($matchingVolumes.Count -gt 0) {
    Log "Found $($matchingVolumes.Count) existing volume(s) named '$VolumeName':"
    foreach ($v in $matchingVolumes) {
        Log ("  - id={0,-12}  dc={1}" -f $v.id, (Get-VolumeDc -Volume $v))
    }
}

$VolumeId = $null
if ($DataCenter -ne 'auto') {
    if ($existingByDc.ContainsKey($DataCenter)) {
        $VolumeId = [string]$existingByDc[$DataCenter].id
        Log "Reusing existing volume in $DataCenter`: $VolumeId"
    } else {
        Log "No existing '$VolumeName' in $DataCenter; will create one."
    }
}

# ─────────────────────────────────────────────────────────────
# Volume-supported DCs (authoritative as of 2026-05-08; refresh from any
# `runpodctl network-volume create` error message — RunPod's error includes
# the live list. EU-SE-1, AP-JP-1, EUR-IS-1, US-GA-2 were dropped between
# 2026-05-01 and 2026-05-08; expect more churn).
# ─────────────────────────────────────────────────────────────
$VolumeSupportedDCs = @(
    'CA-MTL-3','CA-MTL-4','EU-CZ-1','EU-NL-1','EU-RO-1',
    'EUR-IS-3','EUR-NO-1',
    'US-CA-2','US-IL-1','US-KS-2','US-MO-1','US-MO-2',
    'US-NC-2','US-NE-1','US-TX-3','US-WA-1'
)

if ($DataCenter -eq 'auto') {
    Log "Scanning datacenters for stock of '$GpuId'"
    $rawJson = (& runpodctl datacenter list -o json 2>$null) | Out-String
    $dcList = ConvertFrom-RunpodJson -Json $rawJson
    Log "  parsed $($dcList.Count) datacenters"

    $rank = @{ 'High' = 1; 'Medium' = 2; 'Low' = 3 }
    $candidates = New-Object System.Collections.ArrayList
    foreach ($dc in $dcList) {
        # Both -AsHashtable and JSS produce dictionaries indexable via [key].
        $dcId       = [string]$dc['id']
        $dcLocation = [string]$dc['location']
        $gpuAvail   = $dc['gpuAvailability']
        if (-not $gpuAvail) { continue }

        $entry = $null
        foreach ($g in $gpuAvail) {
            if ([string]$g['gpuId'] -eq $GpuId) { $entry = $g; break }
        }
        if (-not $entry) { continue }

        $status = [string]$entry['stockStatus']
        if ([string]::IsNullOrWhiteSpace($status)) { continue }
        if ($status -eq 'Unavailable') { continue }

        $sortKey = 99
        if ($rank.ContainsKey($status)) { $sortKey = $rank[$status] }
        $supportsVolume = $VolumeSupportedDCs -contains $dcId

        $obj = [PSCustomObject]@{
            Id              = $dcId
            Location        = $dcLocation
            Stock           = $status
            SortKey         = [int]$sortKey
            SupportsVolume  = [bool]$supportsVolume
        }
        [void]$candidates.Add($obj)
    }

    $volumeCapable = @($candidates | Where-Object { $_.SupportsVolume })
    if ($volumeCapable.Count -eq 0) {
        Warn "No volume-capable datacenter is currently reporting stock for '$GpuId'."
        Warn "Run with -DataCenter <id> to override. Aborting (don't fall back to a blind default — last time we did, the volume got created in a stockless DC and orphaned)."
        Die "No stock found."
    } else {
        $sorted = @($volumeCapable | Sort-Object SortKey, Id)
        Log "Volume-capable candidate DCs (sorted High > Medium > Low):"
        foreach ($c in $sorted) {
            Log ("  - {0,-10}  loc={1,-15}  stock={2}" -f $c.Id, $c.Location, $c.Stock)
        }
        $DataCenter = [string]$sorted[0].Id
        Log "Picked: $DataCenter ($($sorted[0].Stock) stock)"
    }

    if ($existingByDc.ContainsKey($DataCenter)) {
        $VolumeId = [string]$existingByDc[$DataCenter].id
        Log "Found existing '$VolumeName' in $DataCenter`: $VolumeId  (will reuse)"
    }

    $nonVolHighStock = @($candidates | Where-Object { -not $_.SupportsVolume -and $_.Stock -eq 'High' })
    if ($nonVolHighStock.Count -gt 0) {
        Log "FYI - these DCs have High stock but don't support network volumes:"
        foreach ($c in $nonVolHighStock) { Log ("  (skipped) {0}  stock={1}" -f $c.Id, $c.Stock) }
    }
}

# Sanity-check
$DataCenter = ([string]$DataCenter).Trim()
if ($DataCenter -match '\s' -or $DataCenter -match ',') {
    Die "Internal bug: \$DataCenter contains whitespace or a comma: '$DataCenter'. Aborting."
}
if (-not ($DataCenter -match '^[A-Za-z0-9\-]+$')) {
    Die "Internal bug: \$DataCenter looks malformed: '$DataCenter'. Aborting."
}
Log "Using DATA_CENTER_ID=$DataCenter"

# Now create the volume if we didn't already find one
if (-not $VolumeId) {
    Log "Creating volume: name=$VolumeName size=${VolumeSizeGB}GB dc=$DataCenter"
    $createOut = (runpodctl network-volume create `
        --name $VolumeName `
        --size $VolumeSizeGB `
        --data-center-id $DataCenter `
        -o json 2>&1) | Out-String
    try {
        $created = $createOut | ConvertFrom-Json
        $VolumeId = $created.id
    } catch {
        Die "Volume create returned non-JSON: $createOut"
    }
    if (-not $VolumeId) { Die "Volume create failed: $createOut" }
    Log "Created volume: $VolumeId"
}

# ─────────────────────────────────────────────────────────────
# Pod — refuse duplicates
# ─────────────────────────────────────────────────────────────
$podList = @()
try {
    $podList = @((runpodctl pod list -o json 2>$null) | ConvertFrom-Json)
} catch { $podList = @() }

$existingPod = $podList | Where-Object { $_.name -eq $PodName } | Select-Object -First 1
if ($existingPod) {
    Warn "Pod '$PodName' already exists (id=$($existingPod.id), status=$($existingPod.desiredStatus))."
    Warn "Refusing to create a duplicate. Stop or rename, or pass -PodName <other>."
    return
}

# Build env JSON via PowerShell — never echo the value.
# HF_TOKEN included only if set in the local shell.
$envHash = @{
    PYTORCH_CUDA_ALLOC_CONF = 'expandable_segments:True'
}
if ($env:HF_TOKEN) { $envHash['HF_TOKEN'] = $env:HF_TOKEN }
$envJson = $envHash | ConvertTo-Json -Compress

# PowerShell's legacy native-arg passing strips double quotes from arguments
# bound for external .exe files. Escape each `"` as `\"` so the runpodctl
# process receives a literal double quote.
$envJsonArg = $envJson -replace '"', '\"'

# ─────────────────────────────────────────────────────────────
# Create the pod (with retry — stock fluctuates)
# ─────────────────────────────────────────────────────────────
Log "Creating pod: name=$PodName gpu=$GpuId x$GpuCount dc=$DataCenter"
Log "  image=$PodImage"
Log "  volume=$VolumeId mount=/workspace size=${VolumeSizeGB}GB"

$PodId = $null
$podOut = $null
for ($attempt = 1; $attempt -le 6; $attempt++) {
    if ($attempt -gt 1) {
        Log "Retry attempt $attempt/6 after 30s backoff"
        Start-Sleep -Seconds 30
    }
    $podOut = (runpodctl pod create `
        --name $PodName `
        --cloud-type $CloudType `
        --gpu-id $GpuId `
        --gpu-count $GpuCount `
        --container-disk-in-gb $ContainerDiskGB `
        --data-center-ids $DataCenter `
        --network-volume-id $VolumeId `
        --volume-mount-path '/workspace' `
        --image $PodImage `
        --env $envJsonArg `
        --ssh `
        --ports '22/tcp,8000/http' `
        -o json 2>&1) | Out-String

    try {
        $pod = $podOut | ConvertFrom-Json
        if ($pod.id) { $PodId = $pod.id; break }
    } catch {}

    if ($podOut -match 'no longer any instances available' -or
        $podOut -match 'no instances available' -or
        $podOut -match 'capacity') {
        Warn "Stock unavailable in $DataCenter on attempt $attempt. Will retry."
        continue
    }
    Die "Pod create failed (attempt $attempt): $podOut"
}

if (-not $PodId) {
    Warn "Pod create failed after 6 attempts. The DC '$DataCenter' has no '$GpuId' stock right now."
    Warn "Options:"
    Warn "  1. Wait and re-run later."
    Warn "  2. Delete the network volume and re-run; auto-pick may choose a DC with current stock."
    Warn "  3. Manually pick another volume-supported DC: .\scripts\deploy.ps1 -DataCenter <id>"
    Die "All retries exhausted: $podOut"
}

Log "Pod created: $PodId"
Log "Waiting for pod to enter RUNNING with SSH ready..."

# ─────────────────────────────────────────────────────────────
# Poll for SSH readiness. The new runpodctl shape exposes connection details
# at $podInfo.ssh.{ip,port,ssh_command} — NOT the older $podInfo.runtime.ports[].
# Image pulls on first-time DCs can take 5-10 min; cap at ~5 min here.
# ─────────────────────────────────────────────────────────────
$podInfo = $null
$sshReady = $false
for ($i = 1; $i -le 30; $i++) {
    try {
        $podInfo = (runpodctl pod get $PodId -o json 2>$null) | ConvertFrom-Json
        if ($podInfo.ssh -and -not $podInfo.ssh.error) { $sshReady = $true; break }
    } catch { }
    Start-Sleep -Seconds 10
}

# ─────────────────────────────────────────────────────────────
# Print connection info
# ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[deploy] DONE" -ForegroundColor Green
Write-Host "  pod id:     $PodId"
Write-Host "  status:     $(if ($podInfo) { $podInfo.desiredStatus } else { '?' })"
Write-Host "  volume id:  $VolumeId"
Write-Host ""

if ($sshReady -and $podInfo.ssh.ip -and $podInfo.ssh.port) {
    Write-Host "SSH:"
    if ($podInfo.ssh.ssh_command) {
        Write-Host "  $($podInfo.ssh.ssh_command)"
    } else {
        Write-Host "  ssh -i $($podInfo.ssh.ssh_key.path) -p $($podInfo.ssh.port) root@$($podInfo.ssh.ip)"
    }
} else {
    Write-Host "SSH:"
    Write-Host "  pod RUNNING but SSH not yet ready after $i polls (image probably still pulling)."
    Write-Host "  Re-query in a minute: runpodctl pod get $PodId -o json | ConvertFrom-Json | Select-Object -Expand ssh"
}
Write-Host ""
Write-Host "REMINDER: SSH port may change on stop/start. Always re-query before re-connecting."
Write-Host ""
Write-Host "Next steps (on the pod):"
Write-Host "  cd /workspace"
Write-Host "  git clone https://github.com/voxeloai/4DAnyone.git"
Write-Host "  cd 4DAnyone && git checkout voxelo/main"
Write-Host "  REPO_REMOTE=https://github.com/voxeloai/4DAnyone.git bash scripts/bootstrap.sh"
Write-Host "  source /workspace/activate.sh"
Write-Host ""
Write-Host "For pod-shell sub-agent driving:"
Write-Host "  Spawn architect's pod-shell sub-agent with the SSH details above + cycle goal in .architect/task.md"
