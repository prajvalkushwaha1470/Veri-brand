"""Start VeriBrand locally:  python run.py   ->  http://127.0.0.1:8000"""
import os, uvicorn
if __name__ == '__main__':
    uvicorn.run('backend_main:app', host=os.getenv('HOST', '127.0.0.1'), port=int(os.getenv('PORT', '8000')), reload=False)
