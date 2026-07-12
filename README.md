# watch_up_server
Stock Market Simulator server

## 의존성
**requirements.txt**
```
fastapi==0.128.0
uvicorn[standard]==0.40.0

yfinance==1.0

sqlalchemy==2.0.46
alembic==1.18.3
psycopg[binary]==3.3.2

redis==7.1.0

pydantic==2.12.5
pydantic-settings==2.12.0
email-validator==2.3.0

PyJWT==2.11.0
pwdlib[argon2]==0.3.0

httpx==0.28.1
python-multipart==0.0.22
```

**requirements-dev.txt**
```
-r requirements.txt

pytest==9.0.2
pytest-asyncio==1.3.0
pytest-cov==7.0.0

ruff==0.15.0
mypy==1.19.1
pre-commit==4.5.1
```