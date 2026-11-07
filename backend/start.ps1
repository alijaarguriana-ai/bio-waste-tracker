# Bio Waste Tracker backend: powershell -ExecutionPolicy Bypass -File .\start.ps1
Set-Location -Path $PSScriptRoot

$env:BWT_LLM_MODEL    = "qwen3-vl:2b"
$env:BWT_VISION_MODEL = "qwen3-vl:2b"
$env:BWT_CAM_URL      = "http://172.20.10.3:8080/shot.jpg"   # IP Webcam on the Android phone

# Use the Python that has the dependencies (plain "python" may point to another virtualenv)
$py = "C:\Python314\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

& $py -m uvicorn main:app --host 0.0.0.0 --port 8000
