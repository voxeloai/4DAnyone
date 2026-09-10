<#
.SYNOPSIS
  Copy the licence-gated SMPL-X archive to the fdanyone pod and install it.

.DESCRIPTION
  SMPL-X (https://smpl-x.is.tue.mpg.de/) requires a personal registration + licence acceptance, so
  nobody but the licensee downloads it. Once you have models_smplx_v1_1.zip (or SMPLX_NEUTRAL.npz),
  run this script: it scp's the file to /workspace/smplx/ on the pod and re-runs the bootstrap step
  that installs it into /workspace/models/body_models/smplx/.

.EXAMPLE
  .\scripts\push_smplx.ps1 -Archive "$env:USERPROFILE\Downloads\models_smplx_v1_1.zip"
  .\scripts\push_smplx.ps1 -Archive ..\..\Agents\Architect\env\models_smplx_v1_1.zip -PodId v7h27p8gn1so9i
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Archive,
    [string] $PodId = 'v7h27p8gn1so9i',
    [string] $KeyPath = "$env:USERPROFILE\.runpod\ssh\RunPod-Key-Go"
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path $Archive)) { throw "archive not found: $Archive" }
$pod = runpodctl pod get $PodId -o json | ConvertFrom-Json
if (-not $pod.ssh.ip) { throw "pod $PodId has no SSH endpoint (is it running?)" }
$ip = $pod.ssh.ip; $port = $pod.ssh.port
$name = Split-Path $Archive -Leaf
Write-Host "[smplx] uploading $name to root@${ip}:${port}:/workspace/smplx/"
scp -i $KeyPath -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -P $port $Archive "root@${ip}:/workspace/smplx/$name"
Write-Host "[smplx] installing on the pod"
ssh -i $KeyPath -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -p $port "root@$ip" "source /workspace/activate.sh && python scripts/download_smplx.py --archive_path /workspace/smplx/$name --model_dir /workspace/models --gvhmr_root /workspace/4DAnyone/third_party/GVHMR && ls -la /workspace/models/body_models/smplx/ && curl -s http://127.0.0.1:8000/api/v1/health"
Write-Host "`n[smplx] done. Queued jobs will now process; failed ones can be re-uploaded."
