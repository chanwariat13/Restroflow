"""
run.py - Start RestroFlow
  python run.py           → start server
  python run.py --setup   → create master DB (run once)
"""
import sys

if "--setup" in sys.argv:
    from app.models.database import setup_master_db
    setup_master_db()
    print("✅ Master DB ready. Now add clients via POST /admin/clients")
    sys.exit(0)

import uvicorn
if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False, workers=1)
