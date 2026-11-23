# 🏥 AI Clinic Receptionist (Voice + Twilio + Gemini + Telegram + Excel)

An industry-grade AI phone receptionist for doctor clinics and hospitals.
Patients call a phone number, and an intelligent AI receptionist (Maya) converses with them naturally, checks doctor slot availability, collects patient details step-by-step, writes confirmed bookings directly to an **Excel sheet**, and alerts via **Telegram Bot**.

---

## 🌟 Key Features

1. **Zero Rigid Regex / Zero Hardcoded Trees**:
   - Uses **Gemini 3.5 Flash** with **Function Calling (Tool Use)**.
   - Natural, empathetic, multi-turn conversation (Indian English / Hinglish).
   - Handles interruptions, greetings ("Namaste"), and clinic questions (fees, timings, parking) without losing context.

2. **Real-Time Doctor Availability & Excel Sync**:
   - Every appointment automatically appends to [`data/appointments.xlsx`](data/appointments.xlsx).
   - Checks booked slots so double bookings never occur.

3. **Instant Telegram Alerts**:
   - Connected to `@Frontt_Desk_Bot`.
   - Sends rich formatted notifications with patient name, phone number, symptoms, and booking ID.

4. **Twilio Voice Integration**:
   - Supports inbound calls to Twilio number: `+1 (737) 250-8034`.
   - Supports 1-click outbound calls to patient's verified mobile (`+918824466229`).

5. **Built-in Web Dashboard & Chat Simulator**:
   - Test in the browser at `http://localhost:8000/`.
   - Live appointments table auto-refreshed from Excel.

---

## 🚀 How to Run

### Step 1: Start the Server
Double click `start.bat` or run in terminal:
```bash
python -m uvicorn app.main:app --port 8000 --reload
```

### Step 2: Start Ngrok (To expose localhost to Twilio)
In a separate terminal:
```bash
ngrok http 8000
```
Copy the Forwarding URL (e.g. `https://xxxx-xx.ngrok-free.app`).

### Step 3: Configure Twilio Console
1. Open [Twilio Console](https://console.twilio.com/).
2. Go to **Phone Numbers** ➔ **Manage** ➔ **Active Numbers** ➔ Click `+17372508034`.
3. Under **Voice Configuration** ➔ **"A CALL COMES IN"**:
   - Set to: **Webhook**
   - URL: `https://<YOUR-NGROK-URL>/voice/incoming`
   - Method: **HTTP POST**
4. Click **Save**.

### Step 4: Activate Telegram Alerts
Open Telegram and search for: **`@Frontt_Desk_Bot`**  
Tap **Start** (or send any message). The backend will auto-detect your chat ID!

### Step 5: Test Call
- **Option A (Browser Dashboard):** Open `http://localhost:8000/` in browser, paste your Ngrok URL, and click **"Call My Phone Now"**. Twilio will call your phone `+918824466229`!
- **Option B (Direct Dial):** Dial `+17372508034` from your verified phone.
