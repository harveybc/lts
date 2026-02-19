"""
web.py

Web interface for the LTS (Live Trading System) using AdminLTE dashboard.
Production-grade security: JWT auth, RBAC, rate limiting, CORS, input validation.
"""

from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session as DBSession
from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List
from datetime import datetime, timezone, timedelta
from app.database import get_db, User, Session as UserSession, AuditLog, Config, Statistics, Portfolio, Asset, Order, Position, BillingRecord
import os
import secrets
import json

from jose import jwt, JWTError
import bcrypt

# --- Configuration ---
JWT_SECRET = os.environ.get("LTS_JWT_SECRET", secrets.token_hex(32))
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 7
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 30
ALLOWED_ORIGINS = os.environ.get("LTS_CORS_ORIGINS", "http://localhost:8000,http://localhost:3000").split(",")

# Rate limiting store: {key: [(timestamp), ...]}
_rate_limit_store = {}
RATE_LIMIT_REQUESTS = 60  # per minute
RATE_LIMIT_WINDOW = 60  # seconds

# Create FastAPI app
app = FastAPI(title="LTS Web Dashboard", description="Live Trading System Web Interface")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
    max_age=600,
)

# Setup templates and static files
_templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=_templates_dir)
_static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.exists(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# --- Security Utilities ---

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return False

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    to_encode["exp"] = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode["type"] = "access"
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)

def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    to_encode["exp"] = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode["type"] = "refresh"
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None

def get_current_user(request: Request, db: DBSession = Depends(get_db)) -> User:
    """Extract and validate JWT from Authorization header"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth[7:]
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.query(User).filter(User.username == payload.get("sub")).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user

def require_role(*roles):
    """RBAC decorator factory"""
    def role_checker(current_user: User = Depends(get_current_user)):
        if current_user.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient permissions. Required: {', '.join(roles)}"
            )
        return current_user
    return role_checker

def check_rate_limit(request: Request):
    """Simple rate limiter"""
    key = request.client.host if request.client else "unknown"
    now = datetime.now(timezone.utc).timestamp()
    entries = _rate_limit_store.get(key, [])
    entries = [t for t in entries if now - t < RATE_LIMIT_WINDOW]
    if len(entries) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    entries.append(now)
    _rate_limit_store[key] = entries

# --- Pydantic Models ---

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=128)

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_]+$")
    email: str = Field(..., pattern=r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
    password: str = Field(..., min_length=8, max_length=128)

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class PortfolioCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    total_capital: float = Field(0.0, ge=0)

class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    role: str
    is_active: bool

# --- Security Headers Middleware ---

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

# --- Request Size Limit Middleware ---

@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 1_000_000:  # 1MB
        return JSONResponse(status_code=413, content={"detail": "Request too large"})
    return await call_next(request)

# --- Public Pages ---

@app.get("/", response_class=HTMLResponse)
async def root_page(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/trades", response_class=HTMLResponse)
async def trades_page(request: Request):
    return templates.TemplateResponse("trades.html", {"request": request})

@app.get("/health")
async def health_check():
    return {"status": "ok"}

# --- Auth Endpoints (public) ---

@app.post("/api/auth/login", response_model=TokenResponse)
async def api_login(req: LoginRequest, request: Request, db: DBSession = Depends(get_db)):
    check_rate_limit(request)
    
    user = db.query(User).filter(User.username == req.username).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    # Check lockout
    if hasattr(user, 'locked_until') and user.locked_until and user.locked_until.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc):
        raise HTTPException(status_code=423, detail="Account temporarily locked")
    
    if not verify_password(req.password, user.password_hash):
        # Track failed attempts
        if hasattr(user, 'failed_login_attempts'):
            user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
            if user.failed_login_attempts >= MAX_LOGIN_ATTEMPTS:
                user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
            db.commit()
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    # Reset failed attempts
    if hasattr(user, 'failed_login_attempts'):
        user.failed_login_attempts = 0
        user.locked_until = None
        db.commit()
    
    data = {"sub": user.username, "user_id": user.id, "role": user.role}
    return TokenResponse(
        access_token=create_access_token(data),
        refresh_token=create_refresh_token(data)
    )

@app.post("/api/auth/register")
async def api_register(req: RegisterRequest, request: Request, db: DBSession = Depends(get_db)):
    check_rate_limit(request)
    
    if db.query(User).filter((User.username == req.username) | (User.email == req.email)).first():
        raise HTTPException(status_code=400, detail="Username or email already exists")
    
    # Password complexity
    if not any(c.isupper() for c in req.password):
        raise HTTPException(status_code=400, detail="Password must contain uppercase letter")
    if not any(c.isdigit() for c in req.password):
        raise HTTPException(status_code=400, detail="Password must contain a digit")
    
    user = User(
        username=req.username,
        email=req.email,
        password_hash=hash_password(req.password),
        role="user",
        is_active=True,
        created_at=datetime.now(timezone.utc)
    )
    db.add(user)
    db.commit()
    return {"message": "User registered successfully"}

@app.post("/api/auth/refresh")
async def api_refresh(request: Request, db: DBSession = Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Refresh token required")
    payload = decode_token(auth[7:])
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    user = db.query(User).filter(User.username == payload.get("sub")).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found")
    data = {"sub": user.username, "user_id": user.id, "role": user.role}
    return {"access_token": create_access_token(data), "token_type": "bearer"}

# --- Protected API endpoints ---

@app.get("/api/users/me", response_model=UserResponse)
async def get_profile(current_user: User = Depends(get_current_user)):
    return UserResponse(id=current_user.id, username=current_user.username, email=current_user.email, role=current_user.role, is_active=current_user.is_active)

# Admin-only endpoints
@app.get("/api/users")
async def get_users(current_user: User = Depends(require_role("admin"))):
    db = next(get_db())
    users = db.query(User).all()
    return [{"id": u.id, "username": u.username, "email": u.email, "role": u.role, "is_active": u.is_active} for u in users]

@app.get("/api/audit-logs")
async def get_audit_logs(current_user: User = Depends(require_role("admin")), db: DBSession = Depends(get_db)):
    logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(1000).all()
    return [{"id": l.id, "user_id": l.user_id, "action": l.action, "timestamp": str(l.timestamp), "details": l.details} for l in logs]

@app.get("/api/config")
async def get_config(current_user: User = Depends(require_role("admin")), db: DBSession = Depends(get_db)):
    config = db.query(Config).all()
    return [{"key": c.key, "value": c.value} for c in config]

@app.get("/api/billing")
async def get_billing(current_user: User = Depends(require_role("admin")), db: DBSession = Depends(get_db)):
    records = db.query(BillingRecord).order_by(BillingRecord.timestamp.desc()).limit(1000).all()
    return [{"id": r.id, "user_id": r.user_id, "action_type": r.action_type, "amount": float(r.amount), "timestamp": str(r.timestamp)} for r in records]

# User endpoints (own data only)
@app.get("/api/portfolios")
async def get_portfolios(current_user: User = Depends(get_current_user), db: DBSession = Depends(get_db)):
    if current_user.role == "admin":
        portfolios = db.query(Portfolio).all()
    else:
        portfolios = db.query(Portfolio).filter(Portfolio.user_id == current_user.id).all()
    return [{"id": p.id, "name": p.name, "user_id": p.user_id, "is_active": p.is_active, "total_capital": float(p.total_capital)} for p in portfolios]

@app.post("/api/portfolios")
async def create_portfolio(data: PortfolioCreate, current_user: User = Depends(get_current_user), db: DBSession = Depends(get_db)):
    portfolio = Portfolio(
        user_id=current_user.id,
        name=data.name,
        description=data.description,
        total_capital=data.total_capital,
        is_active=True,
        created_at=datetime.now(timezone.utc)
    )
    db.add(portfolio)
    db.commit()
    return {"id": portfolio.id, "name": portfolio.name}

@app.get("/api/orders")
async def get_orders(current_user: User = Depends(get_current_user), db: DBSession = Depends(get_db)):
    if current_user.role == "admin":
        orders = db.query(Order).all()
    else:
        orders = db.query(Order).filter(Order.user_id == current_user.id).all()
    return [{"id": o.id, "symbol": o.symbol, "order_type": o.order_type, "status": o.status} for o in orders]

@app.get("/api/positions")
async def get_positions(current_user: User = Depends(get_current_user), db: DBSession = Depends(get_db)):
    if current_user.role == "admin":
        positions = db.query(Position).all()
    else:
        # Filter by user's portfolios
        user_portfolio_ids = [p.id for p in db.query(Portfolio).filter(Portfolio.user_id == current_user.id).all()]
        positions = db.query(Position).filter(Position.portfolio_id.in_(user_portfolio_ids)).all()
    return [{"id": p.id, "symbol": p.symbol, "side": p.side, "quantity": float(p.quantity)} for p in positions]

@app.get("/api/statistics")
async def get_statistics(current_user: User = Depends(get_current_user), db: DBSession = Depends(get_db)):
    stats = db.query(Statistics).all()
    return [{"key": s.key, "value": s.value, "timestamp": str(s.timestamp)} for s in stats]

@app.get("/api/billing/me")
async def get_my_billing(current_user: User = Depends(get_current_user), db: DBSession = Depends(get_db)):
    records = db.query(BillingRecord).filter(BillingRecord.user_id == current_user.id).order_by(BillingRecord.timestamp.desc()).all()
    return [{"id": r.id, "action_type": r.action_type, "amount": float(r.amount), "timestamp": str(r.timestamp), "description": r.description} for r in records]

# HTML pages (require auth check in frontend)
@app.get("/portfolios", response_class=HTMLResponse)
async def portfolios_page(request: Request):
    return templates.TemplateResponse("portfolios.html", {"request": request})

@app.get("/assets", response_class=HTMLResponse)
async def assets_page(request: Request):
    return templates.TemplateResponse("assets.html", {"request": request})

@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    return templates.TemplateResponse("users.html", {"request": request})

@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    return templates.TemplateResponse("analytics.html", {"request": request})

@app.get("/portfolios/{portfolio_id}", response_class=HTMLResponse)
async def portfolio_detail_page(request: Request, portfolio_id: int):
    return templates.TemplateResponse("portfolio_detail.html", {"request": request})

@app.get("/assets/{asset_id}", response_class=HTMLResponse)
async def asset_detail_page(request: Request, asset_id: int):
    return templates.TemplateResponse("asset_detail.html", {"request": request})

@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request):
    return templates.TemplateResponse("admin_users.html", {"request": request})

@app.get("/admin/system", response_class=HTMLResponse)
async def admin_system_page(request: Request):
    return templates.TemplateResponse("admin_system.html", {"request": request})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
