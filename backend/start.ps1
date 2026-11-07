# Start the Bio Waste Tracker backend (run from anywhere).
# Optional overrides before running:
#   $env:BWT_LLM_MODEL = "gemma3:4b"; $env:BWT_AI_LANG = "English"
#   $env:BWT_CAM_URL   = "http://172.20.10.5:8080/shot.jpg"
Set-Location -Path $PSScriptRoot
python -m pip install -r requirements.txt --quiet
python -m uvicorn main:app --host 0.0.0.0 --port 8000
