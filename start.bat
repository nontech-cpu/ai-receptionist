@echo off
echo ============================================================
echo   Starting AI Clinic Receptionist Backend (FastAPI)...
echo ============================================================
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
pause
