# ข้อมูลดิบตลาดฝึก 18 พ.ย. 2026

สำเนาของ `logs/` ณ เวลาที่ commit (`logs/` เองอยู่ใน `.gitignore` เพราะ agent เขียนต่อตลอด)

| ไฟล์ | เนื้อหา |
|---|---|
| `decisions.jsonl` | ทุก event ของ agent: `new_game`, `appraisal_sent`, `offers_sent` (ซอง + notes: pressure, k ผู้ซื้อ, spec probe, ladder), `market_closed`, `state_error` |
| `memory-sandbox-<tag>.json` | ต่อเกม: `sent` (ซองที่ส่ง + k + spec) และ `rounds` (ผลซองเรา + `market_summary` ทุก RFQ) |
| `reveal-<เวลา>.json` | หลังปิดตลาด: คุณภาพจริง vs ตัวอย่าง, TV จริง vs ที่ประเมิน, คะแนน, อันดับ (`game_tag` ตรงกับไฟล์ memory) |
| `calendar-1021.json` | กติกาและปฏิทินของค่าตั้งใหม่ |
| `experiments.txt` | เวลาที่เปลี่ยนโค้ดแต่ละครั้ง |

- เกมก่อน 09:10 = ค่าตั้งเก่า (6 รอบ, ไม่มี noise, ไม่มี Specialty) · เกมตั้งแต่ 09:10 = ค่าตั้งใหม่
- เกม `1a781f50` (09:10) เครื่องหลับ พลาด R5–R7 ไม่ควรใช้เทียบ
- อ่านง่าย: `SANDBOX-ROUNDS.md` (รายรอบ) และ `SANDBOX-DATA.md` (สรุป) · สร้างใหม่: `.venv/bin/python tools/rounds_md.py --logs data/sandbox-20261118`
