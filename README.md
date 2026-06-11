# Warteg Bot - Setup Guide

## Files:
- bot.py              - Telegram bot utama
- requirements.txt    - Python dependencies
- .env                - Konfigurasi (isi dengan key kamu)
- google_apps_script.js - Script untuk Google Sheets

## SETUP (lakukan sekali)

### 1. Install Dependencies
Buka PowerShell di folder WartegBot:
```
pip install -r requirements.txt
```

### 2. Setup Google Sheets
1. Buka Google Sheets baru di drive.google.com
2. Klik Extensions > Apps Script
3. Hapus semua kode, paste isi google_apps_script.js
4. Save > Deploy > New Deployment
   - Type: Web app
   - Execute as: Me
   - Who has access: Anyone
5. Copy URL yang diberikan

### 3. Isi file .env
```
TELEGRAM_TOKEN=token_dari_botfather
GEMINI_API_KEY=key_dari_aistudio.google.com
APPS_SCRIPT_URL=url_dari_langkah_2
```

### 4. Jalankan Bot
```
python bot.py
```

## PENGGUNAAN HARIAN
1. Terima foto laporan dari manajer via WA
2. Forward foto ke Telegram Bot
3. Pilih nama cabang
4. Review data yang terbaca AI
5. Klik "Ya Simpan" atau koreksi dulu
6. Data tersimpan otomatis ke Google Sheets

## KOREKSI DATA
Klik "Ada yang Salah" lalu ketik:
```
omzet: 1800000
belanja_pasar: 950000
tanggal: 2026-06-10
```
Lalu ketik /simpan
