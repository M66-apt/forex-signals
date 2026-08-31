# Forex Signal System — เวอร์ชันรันบนคลาวด์ (ไม่ต้องเปิดเครื่องตัวเองทิ้งไว้)

ระบบวิเคราะห์สัญญาณเทรด Forex แบบ rule-based จากอินดิเคเตอร์ทางเทคนิค (EMA crossover,
RSI, MACD, Bollinger Bands)

- **`forex_signals.py`** — ดึงราคา, คำนวณอินดิเคเตอร์, สรุปสัญญาณ BUY/SELL/HOLD,
  เขียนผลลง `signals.json`, ส่งแจ้งเตือน Telegram เมื่อสัญญาณเปลี่ยน
- **`.github/workflows/forex-signals.yml`** — ตัวจับเวลาที่รันสคริปต์ข้างบนให้อัตโนมัติ
  **บนเซิร์ฟเวอร์ของ GitHub** ทุก 15 นาที ตลอด 24 ชม. โดยไม่ต้องเปิดคอมของคุณเลย
- **`dashboard.html`** — เว็บแดชบอร์ดอ่าน `signals.json` แล้วแสดงผล อัปโหลดเป็นเว็บไซต์ฟรีผ่าน
  GitHub Pages ก็เข้าดูจากมือถือ/เครื่องไหนก็ได้

ทั้งหมดนี้ใช้ **GitHub ฟรี** (public repo ใช้ Actions ได้ไม่จำกัดโควตา) ไม่มีค่าใช้จ่าย

## ⚠️ อ่านก่อนใช้งานจริง

- นี่คือเครื่องมือวิเคราะห์เชิงเทคนิค ไม่ใช่เครื่องทำนายราคา ไม่มีระบบไหนบอกอนาคตตลาดได้แม่นยำ
- สัญญาณคำนวณจากกฎที่ตั้งไว้ล่วงหน้า ยังไม่ผ่านการ backtest — ควรทดสอบย้อนหลังและ paper trade
  ก่อนใช้เงินจริงเสมอ
- ข้อมูลจาก API ฟรีเป็นแบบ delayed/rate-limited ไม่ใช่ tick-by-tick แบบโบรกเกอร์
- เพราะเป็น public repo, ไฟล์ `signals.json` (ราคา+สัญญาณ) จะเป็นข้อมูลสาธารณะ — อย่าใส่
  ข้อมูลบัญชีเทรดหรือข้อมูลส่วนตัวใดๆ ลงในโค้ด ให้เก็บ API key ไว้ใน GitHub Secrets เท่านั้น
  (ดูขั้นตอนด้านล่าง ไม่มีคีย์ไหนหลุดไปอยู่ในโค้ดที่ push)
- ผมไม่ใช่ที่ปรึกษาการเงินที่มีใบอนุญาต เนื้อหานี้เพื่อการศึกษาเท่านั้น การตัดสินใจเทรดและ
  ความเสี่ยงทั้งหมดเป็นของคุณ

---

## ขั้นตอนติดตั้ง (ทำครั้งเดียว ใช้เวลา ~10 นาที)

### 1. สร้าง GitHub repo

1. สมัคร/ล็อกอิน https://github.com
2. กด **New repository** → ตั้งชื่อ เช่น `forex-signals` → เลือก **Public** → Create

### 2. อัปโหลดไฟล์ทั้งหมดในโฟลเดอร์นี้เข้า repo

ใช้ได้ทั้งเว็บ (ลาก-วางไฟล์ผ่านปุ่ม "Add file → Upload files") หรือ git บนเครื่อง:

```bash
git init
git add .
git commit -m "init forex signal system"
git branch -M main
git remote add origin https://github.com/<your-username>/forex-signals.git
git push -u origin main
```

> อัปโหลดครั้งเดียวเท่านั้น หลังจากนี้ GitHub จะรันให้เองอัตโนมัติ ไม่ต้อง push อีก

### 3. ขอ API key ฟรีสำหรับข้อมูลราคา (Twelve Data)

1. ไปที่ https://twelvedata.com/pricing กด Free plan แล้วสมัคร
2. คัดลอก API key ที่ได้

### 4. (ไม่บังคับ) ตั้งค่าแจ้งเตือน Telegram

1. คุยกับ [@BotFather](https://t.me/BotFather) ใน Telegram → `/newbot` → ได้ bot token
2. เปิดแชทกับบอทตัวเอง กด Start
3. เปิด `https://api.telegram.org/bot<TOKEN>/getUpdates` ในเบราว์เซอร์ จะเห็น `"chat":{"id": ...}`

### 5. ใส่ค่าเป็น GitHub Secrets (สำคัญ — อย่าใส่คีย์ในโค้ดโดยตรง)

ใน repo ของคุณ: **Settings → Secrets and variables → Actions → New repository secret**
เพิ่มทีละตัว:

| Name | Value |
|---|---|
| `TWELVE_DATA_API_KEY` | API key จากขั้นตอน 3 |
| `TELEGRAM_BOT_TOKEN` | (ถ้าทำขั้นตอน 4) bot token |
| `TELEGRAM_CHAT_ID` | (ถ้าทำขั้นตอน 4) chat id |

### 6. เปิดใช้งาน Actions

ไปแท็บ **Actions** ของ repo → ถ้าขึ้นข้อความให้ enable ก็กด enable →
เลือก workflow **"Update forex signals"** → กด **Run workflow** เพื่อรันรอบแรกด้วยมือ
(ทดสอบว่าทำงานถูกต้อง) หลังจากนั้นมันจะรันเองทุก 15 นาทีตาม cron ที่ตั้งไว้

ถ้ารันสำเร็จ จะเห็นไฟล์ `signals.json` ถูกอัปเดต (commit ใหม่) ใน repo โดยอัตโนมัติ

### 7. เปิดใช้งาน GitHub Pages เพื่อดูแดชบอร์ดออนไลน์

**Settings → Pages** → Source เลือก **Deploy from a branch** → Branch เลือก `main` / `root` → Save

รอ 1-2 นาที แล้วเข้า `https://<your-username>.github.io/forex-signals/dashboard.html`
จากอุปกรณ์ไหนก็ได้ — เว็บจะโหลด `signals.json` ที่อัปเดตจาก Actions มาแสดงผลสด

---

## ปรับความถี่ / คู่เงินที่ติดตาม

แก้ที่ต้นไฟล์ `forex_signals.py`:

| ตัวแปร | ความหมาย |
|---|---|
| `PAIRS` | รายชื่อคู่เงิน เช่น `["EUR/USD", "GBP/USD"]` |
| `INTERVAL` | timeframe แท่งเทียน เช่น `15min`, `1h`, `4h` |
| `OUTPUT_SIZE` | จำนวนแท่งเทียนย้อนหลังที่ใช้คำนวณ |

ความถี่การรันจริงควบคุมด้วย cron ใน `.github/workflows/forex-signals.yml`
(บรรทัด `cron: "*/15 * * * *"`) — แก้เป็น `*/5 * * * *` เพื่อรันทุก 5 นาที ได้ แต่ระวังโควตา
ฟรีของ Twelve Data (8 requests/นาที, 800/วัน) ยิ่งรันถี่ + ยิ่งติดตามหลายคู่เงิน ยิ่งใช้โควตาเร็ว

ตรรกะการให้สัญญาณอยู่ในฟังก์ชัน `generate_signal()` ใน `forex_signals.py` — ปรับน้ำหนัก
คะแนนหรือเพิ่ม/ลดเงื่อนไขได้ตามสไตล์การเทรดของคุณ แล้ว commit/push การแก้ไขเข้า repo
Actions จะใช้เวอร์ชันล่าสุดในรอบถัดไปอัตโนมัติ

---

## อยากรันบนเครื่องตัวเองแทนก็ได้ (ทางเลือกเสริม)

ถ้าเปลี่ยนใจอยากรันเองแบบ real-time ถี่กว่า 15 นาที บนเครื่องที่เปิดทิ้งไว้:

```bash
pip install -r requirements.txt
export TWELVE_DATA_API_KEY=xxxx
python forex_signals.py          # ไม่ใส่ --once จะวนรันต่อเนื่องเอง (loop ตาม POLL_SECONDS)
```

แล้วเปิด `dashboard.html` ผ่าน `python -m http.server` ในโฟลเดอร์เดียวกัน
