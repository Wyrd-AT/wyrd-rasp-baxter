# auth.py
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic()
ADMIN_USER = "admin"
ADMIN_PASS = "wyrd"   # idealmente carregue via variável de ambiente

def authenticate_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct = (credentials.username == ADMIN_USER and credentials.password == ADMIN_PASS)
    if not correct:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais inválidas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
