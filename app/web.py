"""
web.py

Web interface for the LTS (Live Trading System) using AdminLTE dashboard.
This provides a modern, responsive UI for managing portfolios, assets, users,
and system analytics through secure API calls.

:author: LTS Development Team
:copyright: (c) 2025 LTS Project
:license: MIT
"""

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from app.database import get_db, User, Session as UserSession, AuditLog, Config, Statistics, Portfolio, Asset, Order, Position
import os

# Create FastAPI app for web interface
app = FastAPI(title="LTS Web Dashboard", description="Live Trading System Web Interface")

# Setup templates and static files
templates = Jinja2Templates(directory="templates")
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page"""
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page"""
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/portfolios", response_class=HTMLResponse)
async def portfolios_page(request: Request):
    """Portfolios management page"""
    return templates.TemplateResponse("portfolios.html", {"request": request})

@app.get("/assets", response_class=HTMLResponse)
async def assets_page(request: Request):
    """Assets management page"""
    return templates.TemplateResponse("assets.html", {"request": request})

@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    """Users management page"""
    return templates.TemplateResponse("users.html", {"request": request})

@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    """Analytics and statistics page"""
    return templates.TemplateResponse("analytics.html", {"request": request})

# API endpoints for the web interface
@app.get("/api/users")
async def get_users(db: Session = Depends(get_db)):
    """Get all users"""
    users = db.query(User).all()
    return users

@app.get("/api/portfolios")
async def get_portfolios(db: Session = Depends(get_db)):
    """Get all portfolios"""
    portfolios = db.query(Portfolio).all()
    return portfolios

@app.get("/api/assets")
async def get_assets(db: Session = Depends(get_db)):
    """Get all assets"""
    assets = db.query(Asset).all()
    return assets

@app.get("/api/orders")
async def get_orders(db: Session = Depends(get_db)):
    """Get all orders"""
    orders = db.query(Order).all()
    return orders

@app.get("/api/positions")
async def get_positions(db: Session = Depends(get_db)):
    """Get all positions"""
    positions = db.query(Position).all()
    return positions

@app.get("/api/statistics")
async def get_statistics(db: Session = Depends(get_db)):
    """Get system statistics"""
    stats = db.query(Statistics).all()
    return stats

@app.get("/api/config")
async def get_config(db: Session = Depends(get_db)):
    """Get system configuration"""
    config = db.query(Config).all()
    return config

@app.get("/api/audit-logs")
async def get_audit_logs(db: Session = Depends(get_db)):
    """Get audit logs"""
    logs = db.query(AuditLog).all()
    return logs

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
