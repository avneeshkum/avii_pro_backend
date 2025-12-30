import os
from dotenv import load_dotenv

# 🔥 Load Environment Variables (Local testing ke liye)
load_dotenv()

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional
from auth import get_current_user
from sqlalchemy import text

# 🔥 Google Auth Imports
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

# Import Local Modules
import database as db_mod
import auth as auth_mod
import engine as ai_engine

# --- CONFIGURATION ---
# Hosting Dashboard mein 'ADMIN_EMAIL' set karna, nahi to default ye use hoga
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "avneeshsri18@gmail.com")

# --- APP INIT ---
app = FastAPI(title="Avii Pro Advanced Backend (Cloud Ready)")

# Database Tables Create karein
db_mod.Base.metadata.create_all(bind=db_mod.engine)

# CORS Setup (Production mein '*' ki jagah frontend URL daalna safe rehta hai)
origins = [
    "https://avii-pro.netlify.app"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- PYDANTIC SCHEMAS ---
class UserCreate(BaseModel):
    email: str
    password: str

class GoogleToken(BaseModel):
    token: str

class MessageParam(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    session_id: str
    query: str
    history: List[MessageParam] = []
    use_web: bool = True
    # 🔥 Fields for Persona & Creativity
    system_instruction: Optional[str] = "You are Avii Pro."
    temperature: float = 0.3

class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    role: str

# --- HEALTH CHECK ---
@app.get("/")
@app.get("/health")
async def health_check():
    return {
        "status": "active", 
        "message": "Avii Backend is Awake & Ready! 🚀"
    }    

# --- 1. AUTHENTICATION (GOOGLE + EMAIL) ---

# --- 1. GOOGLE LOGIN (Cloud Ready) ---
@app.post("/google-login")
async def google_login(google_data: GoogleToken, db: Session = Depends(db_mod.get_db)):
    try:
        # 1. Google Token Verify
        # Note: Production mein CLIENT_ID verify karna better hota hai
        id_info = id_token.verify_oauth2_token(
            google_data.token, 
            google_requests.Request(),
            os.getenv("GOOGLE_CLIENT_ID")
        )
        email = id_info['email']

        # 2. DB Check
        user = db.query(db_mod.User).filter(db_mod.User.email == email).first()

        if not user:
            # Case A: Naya User
            # Agar email ADMIN_EMAIL se match kare toh 'admin' banao
            role = "admin" if email == ADMIN_EMAIL else "user"
            
            user = db_mod.User(
                email=email, 
                hashed_password="GOOGLE_LOGIN_USER", 
                is_active=True,
                role=role 
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            # Case B: Purana User (Update Role if needed)
            if email == ADMIN_EMAIL and user.role != "admin":
                user.role = "admin"
                db.commit()
                db.refresh(user)

        # 4. Create JWT
        access_token = auth_mod.create_access_token(data={"sub": user.email})
        
        return {
            "access_token": access_token, 
            "token_type": "bearer",
            "user_email": user.email,
            "role": user.role 
        }

    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Google Token")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- 2. REGISTER (Manual Sign up) ---
@app.post("/register", status_code=status.HTTP_201_CREATED)
def register(user: UserCreate, db: Session = Depends(db_mod.get_db)):
    existing_user = db.query(db_mod.User).filter(db_mod.User.email == user.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    hashed_pw = auth_mod.get_password_hash(user.password)
    
    # Manual Register walo ko hamesha 'user' banayenge
    new_user = db_mod.User(
        email=user.email, 
        hashed_password=hashed_pw, 
        role="user"
    )
    db.add(new_user)
    db.commit()
    return {"message": "User created successfully"}


# --- 3. TOKEN (Manual Login) ---
@app.post("/token", response_model=TokenResponse)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(db_mod.get_db)):
    user = db.query(db_mod.User).filter(db_mod.User.email == form_data.username).first()
    if not user or not auth_mod.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")
    
    access_token = auth_mod.create_access_token(data={"sub": user.email})
    
    return {
        "access_token": access_token, 
        "token_type": "bearer",
        "role": user.role
    }

# --- 2. CHAT LOGIC ---

@app.post("/chat")
async def chat_endpoint(
    req: ChatRequest, 
    current_user: db_mod.User = Depends(get_current_user), 
    db: Session = Depends(db_mod.get_db)
):
    # Session Manage
    session = db.query(db_mod.ChatSession).filter(
        db_mod.ChatSession.id == req.session_id, 
        db_mod.ChatSession.user_id == current_user.id
    ).first()
    
    if not session:
        title_summary = req.query[:30] + "..."
        session = db_mod.ChatSession(id=req.session_id, user_id=current_user.id, title=title_summary)
        db.add(session)
        db.commit()

    # Save User Msg
    db.add(db_mod.ChatMessage(session_id=req.session_id, role="user", content=req.query))
    db.commit()

    # AI Process
    history_fmt = [{"role": "user" if m.role == "user" else "assistant", "content": m.content} for m in req.history[-6:]]

    try:
        response_text, source_type = await ai_engine.run_agent(
            query=req.query, 
            history=history_fmt, 
            use_web=req.use_web, 
            user_id=current_user.id,
            system_instruction=req.system_instruction,
            temperature=req.temperature
        )
    except Exception as e:
        print(f"Engine Error: {e}")
        response_text = f"I'm having trouble thinking right now. Error: {e}"
        source_type = "Error"

    # Save AI Response
    db.add(db_mod.ChatMessage(session_id=req.session_id, role="model", content=response_text))
    db.commit()

    return {"response": response_text, "source": source_type, "session_id": req.session_id}

# --- 3. SESSION & HISTORY ---

@app.get("/sessions")
def list_sessions(current_user: db_mod.User = Depends(get_current_user), db: Session = Depends(db_mod.get_db)):
    return db.query(db_mod.ChatSession).filter(db_mod.ChatSession.user_id == current_user.id).order_by(db_mod.ChatSession.created_at.desc()).all()

@app.get("/history/{session_id}")
def get_history(session_id: str, current_user: db_mod.User = Depends(get_current_user), db: Session = Depends(db_mod.get_db)):
    session = db.query(db_mod.ChatSession).filter(db_mod.ChatSession.id == session_id, db_mod.ChatSession.user_id == current_user.id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    msgs = db.query(db_mod.ChatMessage).filter(db_mod.ChatMessage.session_id == session_id).all()
    return [{"role": m.role, "content": m.content} for m in msgs]

@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, current_user: db_mod.User = Depends(get_current_user), db: Session = Depends(db_mod.get_db)):
    session = db.query(db_mod.ChatSession).filter(db_mod.ChatSession.id == session_id, db_mod.ChatSession.user_id == current_user.id).first()
    if session:
        db.delete(session)
        db.commit()
        return {"status": "success", "message": "Chat deleted"}
    raise HTTPException(status_code=404, detail="Not found")

# --- 4. DOCUMENT UPLOAD ---

@app.post("/upload-pdf")
async def upload_pdf(file: UploadFile = File(...), current_user: db_mod.User = Depends(get_current_user)):
    try:
        content = await file.read()
        chunks_count = await ai_engine.ingest_pdf(content, file.filename, current_user.id)
        return {"status": "success", "chunks_indexed": chunks_count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- 5. RESET DB ---
@app.post("/reset-db")
async def reset_db(current_user: db_mod.User = Depends(get_current_user), db: Session = Depends(db_mod.get_db)):
    try:
        db.query(db_mod.ChatSession).filter(db_mod.ChatSession.user_id == current_user.id).delete(synchronize_session=False)
        db.commit()

        await ai_engine.reset_memory() 

        return {"status": "success", "message": "History and PDF Memory cleared!"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

# --- 🔥 6. ADMIN PANEL ENDPOINTS ---

# Admin Verification Function
def get_admin_user(current_user: db_mod.User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="You are not authorized to view this page"
        )
    return current_user

# 6.1 Stats Route
@app.get("/admin/stats")
def get_admin_stats(
    db: Session = Depends(db_mod.get_db), 
    admin: db_mod.User = Depends(get_admin_user)
):
    total_users = db.query(db_mod.User).count()
    total_sessions = db.query(db_mod.ChatSession).count()
    total_messages = db.query(db_mod.ChatMessage).count()
    
    return {
        "total_users": total_users,
        "total_sessions": total_sessions,
        "total_messages": total_messages
    }

# 6.2 Users List Route
@app.get("/admin/users")
def get_all_users(
    db: Session = Depends(db_mod.get_db), 
    admin: db_mod.User = Depends(get_admin_user)
):
    return db.query(db_mod.User).all()

# 🔥 6.3 ADMIN: VIEW USER CHAT HISTORY
@app.get("/admin/user-history/{user_id}")
def get_user_chat_history(
    user_id: int, 
    db: Session = Depends(db_mod.get_db), 
    admin: db_mod.User = Depends(get_admin_user)
):
    # Us user ke saare sessions nikalo
    sessions = db.query(db_mod.ChatSession).filter(db_mod.ChatSession.user_id == user_id).all()
    
    full_history = []
    
    for sess in sessions:
        # Har session ke messages nikalo
        msgs = db.query(db_mod.ChatMessage).filter(db_mod.ChatMessage.session_id == sess.id).all()
        
        full_history.append({
            "session_title": sess.title,
            "created_at": sess.created_at,
            # Timestamp zaroori hai message sorting ke liye
            "messages": [{"role": m.role, "content": m.content, "timestamp": m.timestamp} for m in msgs]
        })
        
    return full_history

if __name__ == "__main__":
    import uvicorn
    # Host '0.0.0.0' allows external access (required for Render/Railway)

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


