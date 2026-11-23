# ⚡ AI Bot Live Quiz Arena (Full-Stack + MCP Workflow)

เว็บแอปพลิเคชัน **AI Bot Live Quiz Arena** แบบ Full-Stack ครบวงจร ดึงคลังความรู้จริงจาก **ROV Academy** 4 หมวดหลัก (`MOBA101`, `แผนที่`, `ไอเทม`, `ตำแหน่ง`) บันทึกลง **Supabase** พร้อมระบบหลังบ้าน **FastAPI (Port 8080)**, **JWT Authentication**, **AI Bot Solver** (คิดแบบสมจริง 2–4 วินาที), **Real-time WebSocket + SSE Console**, **3x Retry + Sentry Error Monitoring**, **Docker Compose**, และ **Unit Tests (`pytest`)**

---

## 🏗️ สถาปัตยกรรมและโครงสร้างไฟล์ (Architecture & File Tree)

```text
ai-bot-live-quiz-arena/
├── .env.example              # เทมเพลตตัวแปรสภาพแวดล้อม (ไม่มีรหัสผ่านจริง)
├── .gitignore                # ป้องกันการ commit .env, .venv และแคช
├── requirements.txt          # รายการไลบรารี Python
├── supabase_schema.sql       # สคริปต์ SQL สร้างตาราง quiz_questions + Index + RLS + RPC
├── scraper.py                # PHASE 1: Playwright Scraper (มี page.pause() รอแก้ Captcha + 3x Retry)
├── server.py                 # PHASE 2: FastAPI Backend (Port 8080) + Static UI Server
├── backend/
│   ├── config.py             # โหลดค่าจาก .env แบบปลอดภัย (Zero Hardcoded Secrets)
│   ├── monitoring.py         # เชื่อมต่อ Sentry SDK (FastApiIntegration + StarletteIntegration)
│   ├── retry_utils.py        # ระบบ Retry 3 ครั้งพร้อม Exponential Backoff + ส่ง Error เข้า Sentry
│   ├── schemas.py            # Pydantic v2 Request/Response Models
│   ├── auth.py               # JWT (HS256) + Timing-Safe Password Verification
│   ├── database.py           # Supabase Repository (SDK + PostgREST HTTPS) พร้อม 3x Retry & Local Fallback
│   ├── ws_manager.py         # Real-time WebSocket ConnectionManager + SSE Fallback Hub
│   └── ai_bot.py             # AI Bot Engine (สุ่มคิด 2–4 วินาที + ยิง Log Real-time)
├── frontend/
│   ├── index.html            # PHASE 3: หน้า Login + Live Quiz Arena (Dark Glassmorphism)
│   ├── style.css             # สไตล์ Dark Glassmorphism, Orbital Thinking Effect, Live Console
│   ├── app.js                # จัดการ JWT Token Bar, Bolt Button, WebSocket & SSE Auto-Fallback
│   ├── Dockerfile            # Nginx Container สำหรับ Frontend
│   └── nginx.conf            # Reverse Proxy /api และ /ws ไปยัง Backend
├── data/
│   └── quiz_questions.json   # คลังคำถาม ROV Academy จริง 4 หมวด (20 ข้อ จาก 15 บทเรียน) สำหรับ Fallback/Seed
├── Dockerfile                # Backend FastAPI Container (Port 8080)
├── docker-compose.yml        # รันทั้ง Backend (:8080) และ Frontend (:3000) พร้อมกัน
├── test_api.py               # ชุด Unit & Integration Test ด้วย pytest
└── README.md                 # คู่มือติดตั้ง ใช้งาน และตั้งค่า Supabase
```

---

## ⚙️ ขั้นตอนที่ 1: การตั้งค่า Supabase (Supabase Setup)

1. สร้างโปรเจกต์ใหม่ที่ [Supabase Dashboard](https://supabase.com/dashboard)
2. ไปที่เมนู **SQL Editor** → กด **New Query**
3. คัดลอกคำสั่งทั้งหมดจากไฟล์ [`supabase_schema.sql`](./supabase_schema.sql) แล้วกด **Run** เพื่อสร้าง:
   - ตาราง `public.quiz_questions` (รองรับ 4 หมวด: `MOBA101`, `แผนที่`, `ไอเทม`, `ตำแหน่ง`)
   - Unique Constraint `external_key` สำหรับ `UPSERT` ไม่ให้ข้อมูลซ้ำ
   - Index, Trigger `updated_at`, Row Level Security (RLS) และฟังก์ชัน `get_random_quiz_question()`
> **ทางเลือกสำหรับ apply schema จาก command line:**
> เพิ่ม `SUPABASE_DB_URL=postgresql://postgres:<db-password>@db.<project-ref>.supabase.co:5432/postgres` ใน `.env` แล้วรัน
> `docker run --rm -i postgres:16-alpine psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 < supabase_schema.sql`

4. ไปที่ **Project Settings → API** แล้วคัดลอกค่าต่อไปนี้ไปใส่ในไฟล์ `.env`:
   - **Project URL** → `SUPABASE_URL`
   - **service_role key** (แนะนำสำหรับ Backend/Scraper เพื่อเขียนข้อมูลได้โดยไม่ติด RLS) หรือ **anon public key** → `SUPABASE_KEY`

---

## 🔐 ขั้นตอนที่ 2: การตั้งค่า Environment Variables (`.env`)

คัดลอกไฟล์ `.env.example` เป็น `.env` แล้วกรอกค่าจริงของคุณ (ห้าม Commit ไฟล์ `.env` ขึ้น Git):

```bash
cp .env.example .env
```

ตัวอย่างตัวแปรสำคัญใน `.env`:

```env
# บัญชีล็อกอินหน้า Arena (ห้าม Hardcode ในโค้ด)
APP_USERNAME=your_arena_username
APP_PASSWORD=your_arena_password

# คีย์ลับสำหรับเซ็น JWT (สร้างด้วย: openssl rand -hex 32)
JWT_SECRET_KEY=your_64_char_random_hex_secret
# อายุ Token (นาที) — dev ตั้ง 1440 = 24 ชั่วโมง, template เริ่มต้น 120
JWT_EXPIRE_MINUTES=1440

# บัญชีล็อกอิน ROV Academy สำหรับ Playwright Scraper
ROV_USERNAME=your_rov_username
ROV_PASSWORD=your_rov_password

# Supabase Credentials
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_KEY=your_supabase_service_role_key
SUPABASE_TABLE=quiz_questions

# Sentry Error Monitoring (ใส่ DSN ของโปรเจกต์ Sentry)
SENTRY_DSN=https://publicKey@o0.ingest.sentry.io/0
```

> **หมายเหตุ:** หากยังไม่ได้กรอก `SUPABASE_URL`/`SUPABASE_KEY` ระบบ Backend จะโหลดคลังคำถามจริงจาก `data/quiz_questions.json` (ที่ดึงมาจาก ROV Academy ทั้ง 4 หมวด) ให้โดยอัตโนมัติ เพื่อให้ทดสอบการทำงานได้ทันที 100%

---

## 🚀 ขั้นตอนที่ 3: การติดตั้งและรันบนเครื่อง (Local Development)

### 1. สร้าง Virtual Environment และติดตั้ง Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. รัน PHASE 1: ดึงข้อมูล ROV Academy ลง Supabase (`scraper.py`)

โหมด Playwright Interactive (เปิดเบราว์เซอร์ กรอกรหัสจาก `.env` แล้วหยุดรอด้วย `page.pause()` เพื่อให้คุณแก้ Captcha เอง):

```bash
python scraper.py
```

หรือโหมด API Sync โดยตรง (ไม่ต้องเปิดหน้าต่างเบราว์เซอร์):

```bash
python scraper.py --api-only
```

> **หมายเหตุ:** scraper จะ merge ข้อมูลตาม `external_key` — คำถามที่ scrape มาใหม่จะอัปเดตทับรายการเดิม แต่คำถามที่เพิ่ม/แก้ไขเองจะไม่ถูกลบ

### 3. รัน PHASE 2 & 3: เปิดเซิร์ฟเวอร์ FastAPI + หน้าเว็บ Live Quiz Arena (Port 8080)

```bash
python server.py
```

เปิดเบราว์เซอร์ไปที่:
- **Live Quiz Arena UI:** `http://127.0.0.1:8080`
- **Swagger API Docs:** `http://127.0.0.1:8080/docs`
- **Health Check:** `http://127.0.0.1:8080/api/health`

---

## 🐳 ขั้นตอนที่ 4: รันด้วย Docker Compose (Frontend + Backend)

สามารถนำโปรเจกต์ไปรันบนเครื่องอื่นได้ทันทีผ่าน Docker:

```bash
cp .env.example .env
# แก้ไขค่าใน .env ให้เรียบร้อย
docker compose up --build -d
```

- **Frontend (Nginx Glassmorphism UI):** `http://localhost:3000`
- **Backend (FastAPI + WebSocket):** `http://localhost:8080`

หยุดการทำงาน:

```bash
docker compose down
```

---

## 🧪 ขั้นตอนที่ 5: รัน Unit Tests (`test_api.py`)

ทดสอบระบบ Login JWT, ดึงคำถามทั้ง 4 หมวด, AI Bot ตอบคำถาม, WebSocket Real-time Log, ระบบ Retry 3 ครั้ง และ Sentry Capture:

```bash
pytest -v test_api.py
```

---

## 🤖 โหมดตอบอัตโนมัติทั้งคลัง (Auto-Run)

หลังเข้าสู่ระบบและได้รับ JWT Token แล้ว ให้กดปุ่ม **`▶ เริ่มตอบทั้งหมด`** ที่แถบเครื่องมือด้านบนของ Quiz Stage:

1. Frontend เรียก `GET /api/questions` (พร้อมตัวกรองหมวดถ้าเลือกไว้) เพื่อ **ดึงคำถามทั้งหมด** จาก Supabase หรือ local cache
2. AI Bot ตอบทีละข้อต่อเนื่องอัตโนมัติ ผ่าน `POST /api/bot/answer` (คิด 2–4 วินาที/ข้อ)
3. แสดงแถบความคืบหน้า `ตอบแล้ว/ทั้งหมด · ความแม่นยำ` และสตรีม Log สดผ่าน WebSocket/SSE ทุกข้อ
4. กดปุ่มเดิมอีกครั้ง (`⏹ หยุดการตอบ`) เพื่อหยุดกลางคันได้ทันที
5. เมื่อจบระบบจะสรุปจำนวนข้อที่ตอบและความแม่นยำลงใน Log Console

---

## 📡 สรุป API Endpoints หลัก

| Method | Endpoint | Auth | คำอธิบาย |
| :--- | :--- | :---: | :--- |
| `POST` | `/api/auth/login` | No | ตรวจสอบบัญชีจาก `.env` และส่งกลับ JWT Token |
| `GET` | `/api/auth/me` | JWT | ตรวจสอบสถานะและข้อมูลผู้ใช้ของ JWT ปัจจุบัน |
| `GET` | `/api/health` | No | เช็กสถานะระบบ, แหล่งข้อมูล (`supabase`/`local_rov_cache`), Sentry และจำนวน WS |
| `GET` | `/api/questions/random` | JWT | สุ่มดึงคำถามจาก Supabase (รองรับ `?category=MOBA101\|แผนที่\|ไอเทม\|ตำแหน่ง`) |
| `GET` | `/api/questions` | JWT | แสดงรายการคำถามทั้งหมดและสถิติแยกตามหมวด |
| `POST` | `/api/bot/answer` | JWT | สั่ง AI Bot คิด 2–4 วินาที เลือกคำตอบที่ถูก และยิง Log สดผ่าน WebSocket/SSE |
| `WS` | `/ws/logs?token=<jwt>` | JWT | สตรีม Real-time Log Console (`[User]`, `[AI]`, `[System]`, `[Error]`) |
| `GET` | `/api/events/stream?token=<jwt>` | JWT | ช่องทาง SSE Fallback อัตโนมัติกรณี Proxy บล็อก WebSocket |
