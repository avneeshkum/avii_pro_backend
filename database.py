import os
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from datetime import datetime, timezone

# --- 1. DATABASE CONNECTION SETUP ---

# 🔥 Step A: Environment se Database URL uthao
# Agar Cloud pe ho to 'DATABASE_URL' milega, nahi to Local 'avii_pro.db' banega.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./avii_pro.db")

# 🔥 Step B: Render/Railway Fix (PostgreSQL URL correction)
# Cloud providers aksar 'postgres://' dete hain, par SQLAlchemy ko 'postgresql://' chahiye.
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# 🔥 Step C: Arguments Logic
# SQLite ko "check_same_thread" chahiye hota hai, par PostgreSQL ko nahi.
connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}

# Engine Create karo
engine = create_engine(
    DATABASE_URL, 
    connect_args=connect_args
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- 2. DATABASE MODELS (TABLES) ---

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    is_active = Column(Boolean, default=True) # User account status
    
    # 🔥 Role Column (Admin Panel ke liye)
    role = Column(String, default="user") 
    
    # User delete hoga toh uske sessions bhi udd jayenge
    sessions = relationship("ChatSession", back_populates="owner", cascade="all, delete-orphan")

class ChatSession(Base):
    __tablename__ = "sessions"
    id = Column(String, primary_key=True, index=True) # Frontend se UUID aayega
    user_id = Column(Integer, ForeignKey("users.id"))
    title = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    owner = relationship("User", back_populates="sessions")
    # Session delete hoga toh uske messages bhi udd jayenge
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")

class ChatMessage(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, ForeignKey("sessions.id"))
    role = Column(String) # 'user' or 'model'
    content = Column(Text)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    session = relationship("ChatSession", back_populates="messages")

# --- 3. CREATE TABLES ---
# Ye line automatic tables bana degi (Local ya Cloud dono jagah)
Base.metadata.create_all(bind=engine)

# --- 4. DEPENDENCY ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()