import os
import asyncio
import io
import uuid
import datetime
from mistralai import Mistral
from tavily import TavilyClient
from pypdf import PdfReader
import chromadb
from chromadb.utils import embedding_functions
from dotenv import load_dotenv

# 🔥 Local dev ke liye .env load karega
load_dotenv()

# --- CONFIGURATION (SECURE FOR HOSTING) ---
# ⚠️ Hardcoded keys hata di hain. Ab ye Environment Variables se aayengi.
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY") 
COHERE_API_KEY = os.getenv("COHERE_API_KEY")

# Init Clients
if not MISTRAL_API_KEY:
    print("❌ ERROR: MISTRAL_API_KEY missing in environment variables!")
    
mistral_client = Mistral(api_key=MISTRAL_API_KEY)
tavily = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None

# --- DATABASE SETUP ---
try:
    # Cohere Embeddings use kar rahe ho, make sure API Key valid ho
    if COHERE_API_KEY:
        cohere_ef = embedding_functions.CohereEmbeddingFunction(
            api_key=COHERE_API_KEY, 
            model_name="embed-english-v3.0"
        )
        db_client = chromadb.PersistentClient(path="./avii_vector_storage")
        collection = db_client.get_or_create_collection(
            name="avii_knowledge_base", 
            embedding_function=cohere_ef
        )
    else:
        # Fallback agar key na ho (Hosting crash na kare)
        print("⚠️ COHERE_API_KEY not found. Vector DB disabled.")
        collection = None

except Exception as e:
    print(f"⚠️ DB Init Error: {e}")
    collection = None

# --- ASYNC HELPER ---
async def run_sync_in_thread(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

# --- INTERNAL FUNCTIONS ---

async def determine_intent(user_query: str) -> str:
    # Quick Check for greetings
    if user_query.lower().strip() in ["hi", "hello", "hey", "sup", "how are you"]:
        return "CHAT"

    prompt = f"""
    Classify the intent of this user query:
    1. WEB: Current events, news, or facts not in a private doc.
    2. PDF: Specific questions about files, documents, or uploaded content.
    3. CHAT: Greetings, coding, math, or general conversation.
    Query: "{user_query}"
    Output ONLY one word: WEB, PDF, or CHAT
    """
    try:
        response = await mistral_client.chat.complete_async(
            model="mistral-small-latest",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        intent = response.choices[0].message.content.strip().upper()
        return intent if intent in ["WEB", "PDF"] else "CHAT"
    except: 
        return "CHAT"

def _search_web_sync(query: str):
    if not tavily: return ""
    try:
        results = tavily.search(query=query, max_results=3, search_depth="advanced")
        context = ""
        for r in results.get('results', []):
            context += f"Source: {r['title']}\nContent: {r['content'][:400]}\n---\n"
        return context
    except: 
        return ""

def _search_pdf_sync(query: str, user_id: str):
    if not collection: return None
    try:
        # metadata filtering for security
        results = collection.query(
            query_texts=[query], 
            n_results=5, 
            where={"user_id": str(user_id)}
        )
        if results['documents'] and len(results['documents'][0]) > 0:
            return "\n".join(results['documents'][0])
    except:
        return None

# --- MEMORY RESET FUNCTION ---
async def reset_memory():
    """ChromaDB ke saare stored chunks ko delete karne ke liye"""
    def clear_all_sync():
        try:
            if collection:
                all_data = collection.get()
                all_ids = all_data.get('ids', [])
                
                if all_ids:
                    collection.delete(ids=all_ids)
                    print(f"✅ ChromaDB Cleared: {len(all_ids)} chunks deleted.")
                    return True
            return False
        except Exception as e:
            print(f"❌ Error during Chroma Reset: {e}")
            return False

    return await run_sync_in_thread(clear_all_sync)

# --- 🚀 MAIN PUBLIC FUNCTIONS (LOGIC UPDATED) ---

async def run_agent(query: str, history: list, use_web: bool, user_id: int, system_instruction: str = None, temperature: float = 0.3):
    
    intent = await determine_intent(query)
    context_data = ""
    source_label = "General Knowledge"

    # Default logic for greeting
    is_greeting = query.lower().strip() in ["hi", "hello", "hey"]

    if not is_greeting:
        if intent == "PDF" or (intent == "CHAT" and collection):
            pdf_data = await run_sync_in_thread(_search_pdf_sync, query, str(user_id))
            if pdf_data:
                context_data = pdf_data
                source_label = "Uploaded Document"

        if not context_data and use_web and (intent == "WEB" or intent == "PDF"):
            web_data = await run_sync_in_thread(_search_web_sync, query)
            if web_data:
                context_data = web_data
                source_label = "Web Search"

    # 🔥 LOGIC UPDATE: Use Custom Instruction if provided, else Default
    base_persona = system_instruction if system_instruction and len(system_instruction) > 5 else "You are Avii Pro, a helpful AI assistant."

    # Construct Final System Prompt (Combining Persona + Context)
    final_system_prompt = f"""
    {base_persona}
    
    CONTEXTUAL INFORMATION:
    - Current Date: {datetime.date.today()}
    - Context Source: {source_label}
    - Retrieved Context: {context_data if context_data else "None (Use internal knowledge)"}
    
    GUIDELINES:
    - If context is provided, prioritize it for your answer.
    - If the user asks for code, provide clean, commented code.
    - If the user asks for math, use LaTeX formatting.
    """

    try:
        msgs = [{"role": "system", "content": final_system_prompt}]
        for h in history:
            msgs.append({"role": h["role"], "content": h["content"]})
        msgs.append({"role": "user", "content": query})

        # 🔥 LOGIC UPDATE: Passing Dynamic Temperature
        response = await mistral_client.chat.complete_async(
            model="mistral-large-latest",
            messages=msgs,
            temperature=temperature 
        )
        
        return response.choices[0].message.content, source_label

    except Exception as e:
        print(f"Agent Error: {e}")
        return f"I encountered an error: {str(e)}", "Error"

async def ingest_pdf(file_content, filename, user_id):
    if not collection: return 0

    def parse_chunk_sync():
        try:
            pdf = PdfReader(io.BytesIO(file_content))
            text = "".join([page.extract_text() + "\n" for page in pdf.pages if page.extract_text()])
            if not text.strip(): return 0

            # Chunking with 1000 size and 200 overlap
            chunks = [text[i:i+1000] for i in range(0, len(text), 800)]
            
            # IDs and Metadata
            ids = [str(uuid.uuid4()) for _ in range(len(chunks))]
            metadatas = [{"user_id": str(user_id), "filename": filename} for _ in range(len(chunks))]
            
            collection.add(documents=chunks, ids=ids, metadatas=metadatas)
            return len(chunks)
        except Exception as e:
            print(f"Ingest Error: {e}")
            return 0

    return await run_sync_in_thread(parse_chunk_sync)