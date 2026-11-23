import os
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import openpyxl

from app.core.config import settings
from app.routes.twilio_routes import router as twilio_router
from app.services.sheet_service import sheet_service
from app.services.telegram_service import telegram_service
from app.services.llm_service import llm_service
from app.services.session_manager import session_manager
from app.services.clinic_service import clinic_service

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="Industry-Grade Voice AI Receptionist with Twilio, Gemini, Telegram & Excel"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(twilio_router)

@app.get("/health")
@app.get("/ping")
def health_check():
    """Lightweight 200 health check endpoint for UptimeRobot and load balancers."""
    return {
        "status": "healthy",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/api/clinic-profile")
def get_clinic_profile():
    """Returns the dynamic enterprise clinic profile."""
    return clinic_service.profile.model_dump()

@app.post("/api/clinic-profile")
async def update_clinic_profile(request: Request):
    """Live updates clinic details (Doctor Name, Fees, Timings, Address, etc.) without restarting server."""
    updates = await request.json()
    updated = clinic_service.update_profile(updates)
    return {
        "status": "success",
        "message": "Clinic profile updated dynamically!",
        "profile": updated.model_dump()
    }

@app.get("/api/appointments")
def get_appointments():
    """Returns recent appointments from Excel for dashboard."""
    sheet_service._ensure_file_exists()
    wb = openpyxl.load_workbook(settings.EXCEL_FILE_PATH, data_only=True)
    ws = wb["Appointments"]
    appointments = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not any(row):
            continue
        appointments.append({
            "booking_id": row[0],
            "timestamp": row[1],
            "patient_name": row[2],
            "phone_number": row[3],
            "date": row[4],
            "time_slot": row[5],
            "symptoms": row[6] if len(row) > 6 else "",
            "duration": row[7] if len(row) > 7 else "",
            "doctor": row[8] if len(row) > 8 else settings.DOCTOR_NAME,
            "status": row[9] if len(row) > 9 else "Confirmed"
        })
    wb.close()
    return list(reversed(appointments))

@app.post("/api/simulator-chat")
async def simulator_chat(request: Request):
    """Allows testing the AI receptionist via browser chat before making phone calls."""
    data = await request.json()
    user_message = data.get("message", "")
    session_id = data.get("session_id", "sim_session_1")

    reply, is_done = llm_service.process_turn(session_id, user_message)
    return {
        "reply": reply,
        "is_done": is_done,
        "session_id": session_id
    }

@app.post("/api/set-telegram-chat-id")
async def set_telegram_chat_id(request: Request):
    """Sets Telegram chat ID, saves to .env, and sends an instant test notification."""
    data = await request.json()
    chat_id = str(data.get("chat_id", "")).strip()
    if not chat_id:
        return {"error": "Chat ID cannot be empty"}

    telegram_service.chat_id = chat_id
    telegram_service._cached_chat_id = chat_id
    telegram_service._persist_chat_id(chat_id)

    # Send instant verification alert
    success = telegram_service._sync_send(
        "🏥 <b>DR. SHARMA CARE CLINIC - BOT CONNECTED</b>\n\n"
        "Namaste! Your Telegram is now officially linked to the AI Phone Receptionist.\n"
        "All confirmed patient booking reports from Twilio phone calls will be delivered here instantly! 📲✅",
        chat_id=chat_id
    )

    # Also send any recent bookings from appointments.xlsx
    appointments = get_appointments()
    if appointments:
        recent = appointments[0]
        telegram_service.send_appointment_alert(recent)

    return {"status": "success", "chat_id": chat_id, "message": "Test notification and latest booking report sent!"}

@app.post("/api/auto-detect-telegram")
async def auto_detect_telegram():
    """Auto-detects any new doctor or user who tapped /start on @Frontt_Desk_Bot."""
    new_chat_id = telegram_service.get_latest_chat_id(force_refresh=True)
    if new_chat_id:
        telegram_service._sync_send(
            "🏥 <b>DR. SHARMA CARE CLINIC - BOT CONNECTED</b>\n\n"
            "Namaste Doctor! Your Telegram is now officially linked to the AI Phone Receptionist.\n"
            "Live appointment reports from calls will now appear right here! 📲",
            chat_id=new_chat_id
        )
        return {"status": "success", "chat_id": new_chat_id, "message": f"Linked to Telegram Chat ID: {new_chat_id}!"}
    return {"status": "error", "message": "No new user detected. Please open @Frontt_Desk_Bot in Telegram and tap 'START' first!"}

@app.get("/api/telegram-status")
def get_telegram_status():
    return {
        "connected": bool(telegram_service.chat_id),
        "chat_id": telegram_service.chat_id,
        "bot_username": "@Frontt_Desk_Bot"
    }

@app.get("/api/audio/{filename}")
def serve_audio(filename: str):
    audio_dir = Path("data/audio_cache")
    file_path = audio_dir / filename
    if file_path.exists():
        return FileResponse(file_path, media_type="audio/mpeg")
    return JSONResponse(status_code=404, content={"error": "Audio file not found"})

@app.get("/architecture-video", response_class=HTMLResponse)
def get_architecture_video():
    video_html_path = Path("data/maya_animated_architecture_video.html")
    if video_html_path.exists():
        return HTMLResponse(content=video_html_path.read_text(encoding="utf-8"))
    fallback_path = Path(r"C:\Users\neera\.gemini\antigravity\brain\56b98b41-a246-4371-b238-25b120dd6549\maya_animated_architecture_video.html")
    if fallback_path.exists():
        return HTMLResponse(content=fallback_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Architecture video not found</h1>", status_code=404)

@app.get("/", response_class=HTMLResponse)
def index():
    """Serves the Management & Testing Dashboard."""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI Receptionist Console - Dr. Sharma Clinic</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen font-sans">
    <div class="max-w-7xl mx-auto p-6 space-y-6">
        
        <!-- Header -->
        <header class="flex flex-col md:flex-row justify-between items-start md:items-center bg-slate-800/80 backdrop-blur p-6 rounded-2xl border border-slate-700/60 shadow-xl">
            <div>
                <div class="flex items-center space-x-3">
                    <span class="p-3 bg-blue-600/20 text-blue-400 rounded-xl border border-blue-500/30">
                        <i class="fa-solid fa-headset text-2xl"></i>
                    </span>
                    <div>
                        <h1 class="text-2xl font-bold tracking-tight text-white">AI Clinic Receptionist</h1>
                        <p class="text-sm text-slate-400">Dr. Sharma's Care Clinic • Twilio + Gemini 3.5 + Telegram + Excel</p>
                    </div>
                </div>
            </div>
            <div class="mt-4 md:mt-0 flex items-center space-x-4">
                <span class="inline-flex items-center px-3 py-1 rounded-full text-xs font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                    <span class="w-2 h-2 rounded-full bg-emerald-400 mr-2 animate-pulse"></span> Twilio Voice Ready
                </span>
                <span class="text-xs text-slate-400 font-mono bg-slate-950 px-3 py-1.5 rounded-lg border border-slate-800">
                    Twilio: <strong class="text-white">+1 (737) 250-8034</strong>
                </span>
            </div>
        </header>

        <!-- Grid Layout: 2 Columns -->
        <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">
            
            <!-- Left Column: Webhook Setup & Trigger Call -->
            <div class="lg:col-span-5 space-y-6">
                
                <!-- Quick Outbound Call Box -->
                <div class="bg-slate-800/70 p-6 rounded-2xl border border-slate-700 shadow-md">
                    <h2 class="text-lg font-semibold text-white flex items-center mb-4">
                        <i class="fa-solid fa-phone-volume text-blue-400 mr-2"></i>
                        Test Live Call on Your Phone
                    </h2>
                    <p class="text-sm text-slate-400 mb-4">
                        Aapke number (<strong class="text-slate-200">+918824466229</strong>) par Twilio se direct call aayegi aur AI receptionist aapse live baat karegi.
                    </p>
                    <div class="space-y-3">
                        <div>
                            <label class="block text-xs font-medium text-slate-400 mb-1">Enter your Ngrok Public URL:</label>
                            <input id="ngrokUrl" type="url" value="https://tightly-barbell-creamed.ngrok-free.dev" placeholder="https://xxxx-xx.ngrok-free.app" 
                                class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-blue-500">
                        </div>
                        <button onclick="triggerCall()" id="callBtn"
                            class="w-full py-2.5 px-4 bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-500 hover:to-indigo-500 text-white font-medium rounded-xl shadow-lg shadow-blue-500/20 transition flex items-center justify-center space-x-2">
                            <i class="fa-solid fa-phone"></i>
                            <span>Call My Phone Now</span>
                        </button>
                        <p id="callStatus" class="text-xs text-center font-medium mt-2 hidden"></p>
                    </div>
                </div>

                <!-- Dynamic Enterprise Clinic Profile Box -->
                <div class="bg-slate-800/70 p-6 rounded-2xl border border-slate-700 shadow-md">
                    <div class="flex items-center justify-between mb-3">
                        <h2 class="text-sm font-semibold text-white flex items-center">
                            <i class="fa-solid fa-hospital text-emerald-400 mr-2"></i>
                            Dynamic Clinic Profile (No Hardcoding)
                        </h2>
                        <span class="text-[10px] bg-emerald-500/10 text-emerald-300 px-2 py-0.5 rounded border border-emerald-500/30">Live Enterprise</span>
                    </div>
                    <p class="text-xs text-slate-400 mb-3">
                        Manager demo ke waqt doctor ka naam, fees aur timings bina code chhue yahan se live badal sakte hain.
                    </p>
                    <div class="space-y-2 text-xs">
                        <div>
                            <label class="text-[11px] text-slate-400 font-medium">Clinic Name:</label>
                            <input id="profClinicName" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-slate-200 text-xs focus:outline-none focus:border-emerald-500">
                        </div>
                        <div class="grid grid-cols-2 gap-2">
                            <div>
                                <label class="text-[11px] text-slate-400 font-medium">Doctor Name:</label>
                                <input id="profDocName" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-slate-200 text-xs focus:outline-none focus:border-emerald-500">
                            </div>
                            <div>
                                <label class="text-[11px] text-slate-400 font-medium">Consultation Fee (₹):</label>
                                <input id="profFee" type="number" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-slate-200 text-xs focus:outline-none focus:border-emerald-500">
                            </div>
                        </div>
                        <div>
                            <label class="text-[11px] text-slate-400 font-medium">Timings:</label>
                            <input id="profTimings" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-slate-200 text-xs focus:outline-none focus:border-emerald-500">
                        </div>
                        <div>
                            <label class="text-[11px] text-slate-400 font-medium">Location / Landmark:</label>
                            <input id="profAddress" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-slate-200 text-xs focus:outline-none focus:border-emerald-500">
                        </div>
                        <button onclick="saveClinicProfile()" id="saveProfBtn" class="w-full py-2 bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg font-medium text-xs transition mt-2 flex items-center justify-center space-x-1 shadow-md shadow-emerald-600/20">
                            <i class="fa-solid fa-floppy-disk text-xs mr-1"></i>
                            <span>Save & Apply Live to AI Voice</span>
                        </button>
                        <p id="profStatus" class="text-[11px] text-center font-medium mt-1 hidden"></p>
                    </div>
                </div>

                <!-- Telegram Integration Status -->
                <div class="bg-slate-800/70 p-6 rounded-2xl border border-slate-700 shadow-md">
                    <h2 class="text-lg font-semibold text-white flex items-center mb-3">
                        <i class="fa-brands fa-telegram text-sky-400 mr-2 text-xl"></i>
                        Telegram Notifications Setup
                    </h2>
                    <p class="text-xs text-slate-400 mb-3 leading-relaxed">
                        Telegram bots cannot message a phone number directly. They deliver reports via <strong>Chat ID</strong>.
                    </p>
                    <div class="space-y-3">
                        <div class="flex items-center space-x-2">
                            <input id="chatIdInput" type="text" placeholder="Enter Telegram Chat ID (e.g. 123456789)" 
                                class="flex-1 bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:border-sky-500">
                            <button onclick="saveChatId()" id="saveTgBtn"
                                class="px-3 py-2 bg-sky-600 hover:bg-sky-500 text-white font-medium rounded-lg text-xs transition flex items-center space-x-1">
                                <span>Link</span>
                            </button>
                            <button onclick="autoDetectTelegram()" id="autoTgBtn"
                                class="px-3 py-2 bg-emerald-600 hover:bg-emerald-500 text-white font-medium rounded-lg text-xs transition flex items-center space-x-1" title="Doctor taps /start on @Frontt_Desk_Bot, then click this to auto-link!">
                                <span>⚡ Auto-Detect</span>
                            </button>
                        </div>
                        <div class="flex items-center justify-between text-[11px] text-slate-400 pt-1">
                            <span>How to get Chat ID? Check <a href="https://t.me/userinfobot" target="_blank" class="text-sky-400 underline">@userinfobot</a></span>
                            <span>Bot: <a href="https://t.me/Frontt_Desk_Bot" target="_blank" class="text-sky-400 underline">@Frontt_Desk_Bot</a></span>
                        </div>
                        <p id="tgAlertStatus" class="text-xs text-center font-medium mt-1 hidden"></p>
                    </div>
                </div>

                <!-- Twilio Webhook Instruction Guide -->
                <div class="bg-slate-800/70 p-6 rounded-2xl border border-slate-700 shadow-md">
                    <h3 class="text-sm font-semibold text-amber-300 flex items-center mb-2">
                        <i class="fa-solid fa-circle-info mr-2"></i> Twilio Console Setup Steps:
                    </h3>
                    <ol class="text-xs text-slate-300 space-y-2 list-decimal list-inside leading-relaxed">
                        <li>Apne terminal me chalao: <code class="bg-slate-900 px-1 py-0.5 rounded text-amber-200">ngrok http 8000</code></li>
                        <li>Ngrok se mila URL copy karo (e.g. <span class="text-blue-300">https://abc.ngrok-free.app</span>)</li>
                        <li>Twilio Console ➔ Phone Numbers ➔ <strong class="text-white">+17372508034</strong> par jao</li>
                        <li>Scroll down to <strong>"A CALL COMES IN"</strong>:</li>
                        <div class="bg-slate-950 p-2.5 rounded-lg border border-slate-800 font-mono text-[11px] text-emerald-400 mt-1">
                            URL: https://&lt;NGROK-URL&gt;/voice/incoming <br/>
                            HTTP: POST
                        </div>
                    </ol>
                </div>
            </div>

            <!-- Right Column: Interactive Chat Simulator & Excel Appointments -->
            <div class="lg:col-span-7 space-y-6">
                
                <!-- Chat Simulator -->
                <div class="bg-slate-800/70 p-6 rounded-2xl border border-slate-700 shadow-md flex flex-col h-[400px]">
                    <div class="flex items-center justify-between pb-3 border-b border-slate-700">
                        <div class="flex items-center space-x-2">
                            <span class="w-2.5 h-2.5 bg-green-400 rounded-full"></span>
                            <h2 class="text-sm font-semibold text-white">Live AI Simulator (Browser Test)</h2>
                        </div>
                        <span class="text-xs text-slate-400">Same prompt as Phone Voice</span>
                    </div>

                    <!-- Messages Container -->
                    <div id="chatMessages" class="flex-1 overflow-y-auto space-y-3 py-3 text-sm">
                        <div class="flex items-start space-x-2">
                            <div class="w-7 h-7 rounded-full bg-blue-600/30 text-blue-400 flex items-center justify-center text-xs flex-shrink-0">
                                AI
                            </div>
                            <div class="bg-slate-700/60 p-3 rounded-2xl rounded-tl-sm text-slate-200 border border-slate-600/40 max-w-[85%]">
                                Namaste! Welcome to Dr. Sharma's Care Clinic. I am Maya, your AI receptionist. How can I help you today?
                            </div>
                        </div>
                    </div>

                    <!-- Chat Input -->
                    <form onsubmit="sendChat(event)" class="pt-3 border-t border-slate-700 flex space-x-2">
                        <input id="chatInput" type="text" placeholder="Type here (e.g. 'I need an appointment tomorrow')..."
                            class="flex-1 bg-slate-900 border border-slate-700 rounded-xl px-4 py-2 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-blue-500">
                        <button type="submit" id="sendBtn"
                            class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-xl text-sm font-medium transition flex items-center space-x-1">
                            <span>Send</span>
                            <i class="fa-solid fa-paper-plane text-xs"></i>
                        </button>
                    </form>
                </div>

                <!-- Recent Appointments Table (Live from Excel) -->
                <div class="bg-slate-800/70 p-6 rounded-2xl border border-slate-700 shadow-md">
                    <div class="flex items-center justify-between mb-4">
                        <h2 class="text-lg font-semibold text-white flex items-center">
                            <i class="fa-solid fa-file-excel text-emerald-400 mr-2 text-xl"></i>
                            Live Appointments (<code class="text-xs text-slate-400 font-mono">appointments.xlsx</code>)
                        </h2>
                        <button onclick="loadAppointments()" class="text-xs text-slate-400 hover:text-white flex items-center space-x-1">
                            <i class="fa-solid fa-rotate"></i>
                            <span>Refresh</span>
                        </button>
                    </div>

                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs">
                            <thead class="text-slate-400 bg-slate-900/60 uppercase text-[10px] tracking-wider">
                                <tr>
                                    <th class="p-2.5 rounded-l-lg">ID</th>
                                    <th class="p-2.5">Patient</th>
                                    <th class="p-2.5">Phone</th>
                                    <th class="p-2.5">Date & Slot</th>
                                    <th class="p-2.5">Reason</th>
                                    <th class="p-2.5 rounded-r-lg">Status</th>
                                </tr>
                            </thead>
                            <tbody id="apptTableBody" class="divide-y divide-slate-700/60 text-slate-300">
                                <tr>
                                    <td colspan="6" class="p-4 text-center text-slate-500">Loading appointments...</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>

            </div>
        </div>
    </div>

    <script>
        const sessionId = "web_session_" + Math.random().toString(36).substring(7);

        async function saveChatId() {
            const chatId = document.getElementById('chatIdInput').value.trim();
            const status = document.getElementById('tgAlertStatus');
            const btn = document.getElementById('saveTgBtn');

            if (!chatId) {
                alert('Please enter your Telegram numeric Chat ID (from @userinfobot)');
                return;
            }

            btn.disabled = true;
            btn.innerText = 'Linking...';
            status.className = 'text-xs text-center text-yellow-400 mt-1 block';
            status.innerText = 'Linking and sending test message...';

            try {
                const res = await fetch('/api/set-telegram-chat-id', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({chat_id: chatId})
                });
                const data = await res.json();
                if (data.status === 'success') {
                    status.className = 'text-xs text-center text-emerald-400 mt-1 block';
                    status.innerText = 'Linked! Check your Telegram for confirmation report.';
                } else {
                    status.className = 'text-xs text-center text-red-400 mt-1 block';
                    status.innerText = data.error || 'Failed to send.';
                }
            } catch (e) {
                status.className = 'text-xs text-center text-red-400 mt-1 block';
                status.innerText = 'Network error linking Telegram.';
            } finally {
                btn.disabled = false;
                btn.innerText = 'Link';
            }
        }

        async function autoDetectTelegram() {
            const btn = document.getElementById('autoTgBtn');
            const status = document.getElementById('tgAlertStatus');
            btn.disabled = true;
            btn.innerText = 'Detecting...';
            status.className = 'text-xs text-center text-yellow-400 mt-1 block';
            status.innerText = 'Checking Telegram for latest /start user...';

            try {
                const res = await fetch('/api/auto-detect-telegram', {method: 'POST'});
                const data = await res.json();
                if (data.status === 'success') {
                    document.getElementById('chatIdInput').value = data.chat_id;
                    status.className = 'text-xs text-center text-emerald-400 mt-1 block';
                    status.innerText = '🎉 ' + data.message;
                } else {
                    status.className = 'text-xs text-center text-amber-400 mt-1 block';
                    status.innerText = data.message;
                }
            } catch (e) {
                status.className = 'text-xs text-center text-red-400 mt-1 block';
                status.innerText = 'Error checking Telegram updates.';
            } finally {
                btn.disabled = false;
                btn.innerText = '⚡ Auto-Detect';
            }
        }

        async function loadAppointments() {
            try {
                const res = await fetch('/api/appointments');
                const data = await res.json();
                const tbody = document.getElementById('apptTableBody');
                if (data.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" class="p-4 text-center text-slate-500">No appointments booked yet. Make a call or test in chat!</td></tr>';
                    return;
                }
                tbody.innerHTML = data.map(a => `
                    <tr class="hover:bg-slate-700/30 transition">
                        <td class="p-2.5 font-mono text-blue-400">${a.booking_id}</td>
                        <td class="p-2.5 font-medium text-white">${a.patient_name}</td>
                        <td class="p-2.5">${a.phone_number}</td>
                        <td class="p-2.5 text-emerald-300">${a.date} (${a.time_slot})</td>
                        <td class="p-2.5 text-slate-400 max-w-[150px] truncate">${a.reason}</td>
                        <td class="p-2.5">
                            <span class="px-2 py-0.5 rounded-full text-[10px] bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">
                                ${a.status}
                            </span>
                        </td>
                    </tr>
                `).join('');
            } catch (e) {
                console.error(e);
            }
        }

        async function sendChat(e) {
            e.preventDefault();
            const input = document.getElementById('chatInput');
            const msg = input.value.trim();
            if (!msg) return;

            input.value = '';
            const container = document.getElementById('chatMessages');

            // Add user message
            container.innerHTML += `
                <div class="flex items-start justify-end space-x-2">
                    <div class="bg-blue-600 p-3 rounded-2xl rounded-tr-sm text-white max-w-[85%]">
                        ${msg}
                    </div>
                </div>
            `;
            container.scrollTop = container.scrollHeight;

            const sendBtn = document.getElementById('sendBtn');
            sendBtn.disabled = true;
            sendBtn.innerHTML = '<i class="fa-solid fa-spinner animate-spin"></i>';

            try {
                const res = await fetch('/api/simulator-chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message: msg, session_id: sessionId})
                });
                const data = await res.json();
                container.innerHTML += `
                    <div class="flex items-start space-x-2">
                        <div class="w-7 h-7 rounded-full bg-blue-600/30 text-blue-400 flex items-center justify-center text-xs flex-shrink-0">
                            AI
                        </div>
                        <div class="bg-slate-700/60 p-3 rounded-2xl rounded-tl-sm text-slate-200 border border-slate-600/40 max-w-[85%]">
                            ${data.reply}
                        </div>
                    </div>
                `;
                container.scrollTop = container.scrollHeight;
                loadAppointments();
            } catch (err) {
                container.innerHTML += `<div class="text-xs text-red-400 p-2">Error connecting to AI service.</div>`;
            } finally {
                sendBtn.disabled = false;
                sendBtn.innerHTML = '<span>Send</span> <i class="fa-solid fa-paper-plane text-xs"></i>';
            }
        }

        async function triggerCall() {
            let ngrok = document.getElementById('ngrokUrl').value.trim().replace(/\\.+$/, '').replace(/\\/+$/, '');
            const status = document.getElementById('callStatus');
            const btn = document.getElementById('callBtn');

            if (!ngrok) {
                alert('Please enter your Ngrok URL first (e.g. https://xxxx.ngrok-free.app)');
                return;
            }

            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner animate-spin"></i> Calling...';
            status.className = 'text-xs text-center text-yellow-400 mt-2 block';
            status.innerText = 'Initiating Twilio call to +918824466229...';

            try {
                const res = await fetch(`/api/make-call?to_number=%2B918824466229&webhook_url=${encodeURIComponent(ngrok)}`, {
                    method: 'POST'
                });
                const data = await res.json();
                if (data.status === 'success') {
                    status.className = 'text-xs text-center text-emerald-400 mt-2 block';
                    status.innerText = 'Phone ringing! Pick up the call to talk to Maya.';
                } else {
                    status.className = 'text-xs text-center text-red-400 mt-2 block';
                    status.innerText = data.error || 'Failed to call.';
                }
            } catch (e) {
                status.className = 'text-xs text-center text-red-400 mt-2 block';
                status.innerText = 'Network error initiating call.';
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-phone"></i> <span>Call My Phone Now</span>';
            }
        }

        async function loadClinicProfile() {
            try {
                const res = await fetch('/api/clinic-profile');
                const p = await res.json();
                if (p) {
                    document.getElementById('profClinicName').value = p.clinic_name || '';
                    document.getElementById('profDocName').value = p.doctor_name || '';
                    document.getElementById('profFee').value = p.pricing ? p.pricing.consultation_fee : 500;
                    document.getElementById('profTimings').value = p.timings ? p.timings.working_hours : '';
                    const loc = p.location ? (p.location.address + ', ' + p.location.landmark) : '';
                    document.getElementById('profAddress').value = loc;
                }
            } catch (e) {
                console.error('Failed to load clinic profile', e);
            }
        }

        async function saveClinicProfile() {
            const btn = document.getElementById('saveProfBtn');
            const status = document.getElementById('profStatus');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner animate-spin mr-1"></i> Saving...';
            try {
                const payload = {
                    clinic_name: document.getElementById('profClinicName').value.trim(),
                    doctor_name: document.getElementById('profDocName').value.trim(),
                    pricing: { consultation_fee: parseInt(document.getElementById('profFee').value) || 500 },
                    timings: { working_hours: document.getElementById('profTimings').value.trim() },
                    location: { address: document.getElementById('profAddress').value.trim() }
                };
                const res = await fetch('/api/clinic-profile', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                status.className = 'text-[11px] text-center text-emerald-400 font-medium mt-1 block';
                status.innerText = 'Profile updated live! AI Voice and prompts updated.';
                setTimeout(() => status.classList.add('hidden'), 4000);
            } catch (e) {
                status.className = 'text-[11px] text-center text-red-400 font-medium mt-1 block';
                status.innerText = 'Error saving profile.';
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-floppy-disk text-xs mr-1"></i> <span>Save & Apply Live to AI Voice</span>';
            }
        }

        loadAppointments();
        loadClinicProfile();
        loadTelegramStatus();
        setInterval(loadTelegramStatus, 5000);
        setInterval(loadAppointments, 5000);
    </script>
</body>
</html>
    """
