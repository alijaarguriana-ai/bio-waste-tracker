# Bio Waste Tracker backend
#   powershell -ExecutionPolicy Bypass -File .\start.ps1                       (phone at 172.20.10.3)
#   powershell -ExecutionPolicy Bypass -File .\start.ps1 -PhoneIp 192.168.1.50  (other IP shown by IP Webcam)
param([string]$PhoneIp = "172.20.10.3")
Set-Location -Path $PSScriptRoot

$env:BWT_LLM_MODEL    = "qwen3-vl:2b-instruct"
$env:BWT_VISION_MODEL = "qwen3-vl:2b-instruct"
$env:BWT_CAM_URL      = "http://${PhoneIp}:8080/shot.jpg"   # IP Webcam on the Android phone

# Check the phone camera before starting
try {
    Invoke-WebRequest $env:BWT_CAM_URL -UseBasicParsing -TimeoutSec 4 | Out-Null
    Write-Host "Camera OK: $env:BWT_CAM_URL" -ForegroundColor Green
} catch {
    Write-Host "Camera NOT reachable at $env:BWT_CAM_URL (same Wi-Fi? VPN off? IP Webcam started?)" -ForegroundColor Yellow
}

# Use the Python that has the dependencies (plain "python" may point to another virtualenv)
$py = "C:\Python314\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

& $py -m uvicorn main:app --host 0.0.0.0 --port 8000
