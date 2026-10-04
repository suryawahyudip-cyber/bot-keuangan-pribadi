# bot.py (v4)
# - Hanya melayani pemilik (Telegram ID di ALLOWED_USER_ID), semua orang lain ditolak
# - /ringkasan = bulan berjalan, /ringkasanbulanan = bulan apa saja (bot bertanya dulu)
# - Notifikasi otomatis kalau pengeluaran harian melewati batas (default Rp50.000)
# - AI (Gemini) membantu memahami pesan natural, dengan hasil tetap divalidasi
#
# Format transaksi: <kategori> <jumlah> <akun> <deskripsi opsional>
# Contoh: "makan 15000 bca beli nasi goreng"

import os
import re
import json
import time
import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
import google.generativeai as genai
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from dotenv import load_dotenv

load_dotenv()  # baca file .env kalau ada (untuk development di laptop)

WIB = ZoneInfo("Asia/Jakarta")


def tanggal_wib():
    """Tanggal 'hari ini' menurut waktu Indonesia (WIB), bukan waktu server.
    Server (Railway) biasanya pakai UTC, yang bisa beda hari dengan WIB
    terutama dini hari (00:00-06:59 WIB = masih hari sebelumnya di UTC)."""
    return datetime.now(WIB).date()


def baca_int_env(nama, default=None):
    nilai = os.environ.get(nama, "").strip()
    if not nilai:
        return default
    try:
        return int(nilai.replace(".", "").replace(",", ""))
    except ValueError:
        print(f"[CONFIG] {nama} bukan angka yang valid ({nilai!r}) - diabaikan")
        return default


# --- Konfigurasi dari environment variables ---

TOKEN = os.environ["TELEGRAM_TOKEN"]
SHEET_ID = os.environ["SHEET_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")  # opsional - tanpa ini, bot tetap jalan tanpa AI

# Telegram ID pemilik. Kalau kosong, bot menolak SEMUA orang (kecuali perintah /id,
# supaya kamu bisa mengetahui ID kamu sendiri lalu mengisi variable ini).
ALLOWED_USER_ID = baca_int_env("ALLOWED_USER_ID")

# Batas pengeluaran per hari (Rupiah). Bot memberi peringatan kalau total hari ini melewatinya.
BATAS_HARIAN = baca_int_env("BATAS_HARIAN", 50000)

# Kategori yang dianggap perpindahan dana antar akun, bukan pengeluaran sebenarnya,
# jadi tidak dihitung ke batas harian. Ubah sesuai nama kategori di sheet Kategori kamu.
KATEGORI_TRANSFER = ("Tarik Tunai", "Top Up")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model_ai = genai.GenerativeModel("gemini-3.5-flash-lite")
else:
    model_ai = None


# --- Setup koneksi ke Google Sheets (dijalankan sekali saat bot mulai) ---

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(SHEET_ID)

ws_transaksi = sheet.worksheet("Transaksi")
ws_kategori = sheet.worksheet("Kategori")
ws_akun = sheet.worksheet("Akun")


# =====================================================================
# Utilitas umum
# =====================================================================

NAMA_BULAN = [
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
]


def format_rp(angka):
    try:
        angka = float(angka)
    except (TypeError, ValueError):
        angka = 0
    tanda = "-" if angka < 0 else ""
    return f"{tanda}Rp{abs(angka):,.0f}"


def adalah_transfer(kategori):
    return str(kategori).strip().lower() in {k.lower() for k in KATEGORI_TRANSFER}


def serial_ke_tanggal(nilai):
    """Google Sheets menyimpan tanggal sebagai 'serial number' (hari sejak 30 Des 1899).
    Fungsi ini mengubahnya balik jadi objek date Python, apa pun bentuk nilainya."""
    if isinstance(nilai, bool):
        return None
    if isinstance(nilai, (int, float)):
        return date(1899, 12, 30) + timedelta(days=int(nilai))
    if isinstance(nilai, str) and nilai.strip():
        teks = nilai.strip()
        try:
            return date.fromisoformat(teks)
        except ValueError:
            pass
        try:
            d, m, y = teks.split("/")
            return date(int(y), int(m), int(d))
        except Exception:
            return None
    return None


# =====================================================================
# Membaca & memproses data sheet
# =====================================================================

def ambil_daftar(worksheet, kolom):
    # Ambil semua nilai di satu kolom (mulai baris 5), buang sel kosong
    nilai_mentah = worksheet.col_values(kolom)[4:]  # index 4 = baris 5 (mulai dari 0)
    return [v for v in nilai_mentah if v.strip() != ""]


def ambil_transaksi():
    """Baca seluruh sheet Transaksi (kolom A-G, mulai baris 5) dalam SATU panggilan API.
    Return list of dict: tanggal (date), tipe ('In'/'Out'), kategori, akun, jumlah, deskripsi."""
    baris_semua = ws_transaksi.get("A5:G", value_render_option="UNFORMATTED_VALUE")

    hasil = []
    for baris in baris_semua:
        baris = list(baris) + [""] * (7 - len(baris))
        tanggal = serial_ke_tanggal(baris[0])
        if tanggal is None:
            continue

        tipe = str(baris[2]).strip()
        if tipe.lower() == "out":
            tipe = "Out"
        elif tipe.lower() == "in":
            tipe = "In"

        jumlah = baris[5]
        if isinstance(jumlah, bool) or not isinstance(jumlah, (int, float)):
            try:
                jumlah = float(str(jumlah).strip())
            except ValueError:
                jumlah = 0

        hasil.append({
            "tanggal": tanggal,
            "tipe": tipe,
            "kategori": str(baris[3]).strip(),
            "akun": str(baris[4]).strip(),
            "jumlah": jumlah,
            "deskripsi": str(baris[6]).strip(),
        })
    return hasil


def filter_bulan(transaksi, bulan, tahun):
    return [t for t in transaksi if t["tanggal"].month == bulan and t["tanggal"].year == tahun]


def format_ringkasan(bulan, tahun, transaksi_bulan):
    label = f"{NAMA_BULAN[bulan - 1]} {tahun}"
    if not transaksi_bulan:
        return f"📊 Ringkasan {label}\n\nBelum ada transaksi tercatat di bulan ini."

    total_in = 0
    total_out = 0
    per_kategori = {}
    per_sumber = {}
    for t in transaksi_bulan:
        nama = t["kategori"] or "(tanpa kategori)"
        if t["tipe"] == "Out":
            total_out += t["jumlah"]
            per_kategori[nama] = per_kategori.get(nama, 0) + t["jumlah"]
        elif t["tipe"] == "In":
            total_in += t["jumlah"]
            per_sumber[nama] = per_sumber.get(nama, 0) + t["jumlah"]

    pesan = (
        f"📊 Ringkasan {label} ({len(transaksi_bulan)} transaksi)\n\n"
        f"Pemasukan: {format_rp(total_in)}\n"
        f"Pengeluaran: {format_rp(total_out)}\n"
        f"Saldo Bersih: {format_rp(total_in - total_out)}"
    )
    if per_kategori:
        baris = [f"• {k}: {format_rp(v)}" for k, v in sorted(per_kategori.items(), key=lambda x: -x[1])]
        pesan += "\n\nPengeluaran per Kategori:\n" + "\n".join(baris)
    if per_sumber:
        baris = [f"• {k}: {format_rp(v)}" for k, v in sorted(per_sumber.items(), key=lambda x: -x[1])]
        pesan += "\n\nPemasukan per Sumber:\n" + "\n".join(baris)
    return pesan


def hitung_pengeluaran_harian(transaksi, tanggal):
    """Total pengeluaran (Out) pada satu tanggal, di luar kategori transfer antar akun."""
    return sum(
        t["jumlah"]
        for t in transaksi
        if t["tanggal"] == tanggal and t["tipe"] == "Out" and not adalah_transfer(t["kategori"])
    )


def buat_peringatan_batas(total_hari_ini, jumlah_transaksi_ini, batas):
    """Pesan peringatan kalau total hari ini MELEBIHI batas, selain itu None."""
    if total_hari_ini <= batas:
        return None
    selisih = total_hari_ini - batas
    sebelum = total_hari_ini - jumlah_transaksi_ini
    catatan = f"(di luar transfer: {', '.join(KATEGORI_TRANSFER)})"
    if sebelum <= batas:
        return (
            f"🚨 Pengeluaran hari ini {format_rp(total_hari_ini)} baru saja melewati "
            f"batas harian {format_rp(batas)} (lebih {format_rp(selisih)}) {catatan}"
        )
    return (
        f"⚠️ Pengeluaran hari ini sudah {format_rp(total_hari_ini)}, di atas "
        f"batas harian {format_rp(batas)} (lebih {format_rp(selisih)}) {catatan}"
    )


# =====================================================================
# Memahami bulan dari teks (kata kunci dulu, AI sebagai cadangan)
# =====================================================================

BULAN_ALIAS = {
    "januari": 1, "jan": 1, "january": 1,
    "februari": 2, "pebruari": 2, "feb": 2, "febr": 2, "february": 2,
    "maret": 3, "mar": 3, "march": 3,
    "april": 4, "apr": 4,
    "mei": 5, "may": 5,
    "juni": 6, "jun": 6, "june": 6,
    "juli": 7, "jul": 7, "july": 7,
    "agustus": 8, "agu": 8, "agt": 8, "ags": 8, "agust": 8, "aug": 8, "august": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10, "oct": 10, "october": 10,
    "november": 11, "nov": 11, "nop": 11,
    "desember": 12, "des": 12, "dec": 12, "december": 12,
}

ANGKA_KATA = {
    "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5, "enam": 6,
    "tujuh": 7, "delapan": 8, "sembilan": 9, "sepuluh": 10, "sebelas": 11,
}


def geser_bulan(bulan, tahun, selisih):
    indeks = tahun * 12 + (bulan - 1) + selisih
    return indeks % 12 + 1, indeks // 12


def _bulan_tahun_valid(bulan, tahun):
    return 1 <= bulan <= 12 and 2000 <= tahun <= 2100


def parse_bulan(teks, hari_ini):
    """Pahami bulan dari teks bebas. Return (bulan, tahun) atau None kalau tidak jelas.
    Contoh: 'agustus', 'Agustus 2025', 'bulan lalu', '2 bulan lalu', '08/2026', '2026-08'."""
    t = teks.lower()
    t = re.sub(r"[^\w\s/\-]", " ", t)  # buang tanda baca, sisakan / dan -
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return None

    bulan_now, tahun_now = hari_ini.month, hari_ini.year

    # 1. Format angka: 2026-08, 2026/8, 08/2026, 8-2026
    m = re.search(r"\b(\d{4})[/\-](\d{1,2})\b", t)
    if m and _bulan_tahun_valid(int(m.group(2)), int(m.group(1))):
        return int(m.group(2)), int(m.group(1))
    m = re.search(r"\b(\d{1,2})[/\-](\d{4})\b", t)
    if m and _bulan_tahun_valid(int(m.group(1)), int(m.group(2))):
        return int(m.group(1)), int(m.group(2))

    # Tahun eksplisit (2000-2099) atau "tahun lalu"/"tahun ini"
    m_tahun = re.search(r"\b(20\d{2})\b", t)
    if m_tahun:
        tahun_eksplisit = int(m_tahun.group(1))
    elif re.search(r"\btahun\s+(lalu|kemarin|sebelumnya)\b", t) or "setahun lalu" in t:
        tahun_eksplisit = tahun_now - 1
    elif re.search(r"\btahun\s+ini\b", t):
        tahun_eksplisit = tahun_now
    else:
        tahun_eksplisit = None

    def tahun_untuk(bulan):
        if tahun_eksplisit is not None:
            return tahun_eksplisit
        # Tanpa tahun: ambil kemunculan terakhir yang sudah lewat/sedang berjalan
        return tahun_now if bulan <= bulan_now else tahun_now - 1

    # 2. Nama bulan (agustus, agu, aug, ...)
    for kata in t.split():
        if kata in BULAN_ALIAS:
            bulan = BULAN_ALIAS[kata]
            tahun = tahun_untuk(bulan)
            return (bulan, tahun) if _bulan_tahun_valid(bulan, tahun) else None

    # 3. Relatif: "2 bulan lalu", "dua bulan yang lalu"
    pola_angka = r"(\d{1,2}|" + "|".join(ANGKA_KATA) + r")"
    m = re.search(pola_angka + r"\s+bulan\s+(?:yang\s+)?(?:lalu|kemarin|sebelumnya)\b", t)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else ANGKA_KATA[m.group(1)]
        bulan, tahun = geser_bulan(bulan_now, tahun_now, -n)
        return (bulan, tahun) if _bulan_tahun_valid(bulan, tahun) else None

    # 4. "bulan ini" / "bulan lalu"
    if re.search(r"\b(bulan|bln)\s+(ini|sekarang|berjalan)\b", t) or t in ("ini", "sekarang"):
        return bulan_now, tahun_now
    if re.search(r"\b(bulan|bln)\s+(yang\s+)?(lalu|kemarin|sebelumnya)\b", t) or t in ("lalu", "kemarin", "sebelumnya"):
        return geser_bulan(bulan_now, tahun_now, -1)

    # 5. Nomor bulan: "bulan 8" atau cukup "8"
    m = re.search(r"\b(?:bulan|bln)\s+(\d{1,2})\b", t)
    if m is None and re.fullmatch(r"\d{1,2}", t):
        m = re.fullmatch(r"(\d{1,2})", t)
    if m and 1 <= int(m.group(1)) <= 12:
        bulan = int(m.group(1))
        tahun = tahun_untuk(bulan)
        return (bulan, tahun) if _bulan_tahun_valid(bulan, tahun) else None

    return None


# =====================================================================
# AI (Gemini)
# =====================================================================

async def panggil_ai_json(prompt):
    """Kirim prompt ke Gemini, harapannya balasan berupa JSON murni.
    Return dict, atau None kalau AI tidak tersedia / gagal / balasan bukan JSON."""
    if model_ai is None:
        return None
    try:
        respons = await asyncio.wait_for(
            asyncio.to_thread(model_ai.generate_content, prompt), timeout=25
        )
        teks_respons = respons.text.strip()
        teks_respons = teks_respons.replace("```json", "").replace("```", "").strip()
        data = json.loads(teks_respons)
    except Exception as e:
        print(f"[AI] Gagal memanggil/memparsing AI: {type(e).__name__}: {e}")
        return None
    return data if isinstance(data, dict) else None


async def parse_dengan_ai(teks, daftar_pengeluaran, daftar_pemasukan, daftar_akun):
    """Minta Gemini mengekstrak data transaksi dari pesan bebas (bahasa natural).
    Return dict mentah dari AI, atau None kalau gagal/AI tidak tersedia."""
    prompt = f"""Kamu mengekstrak data transaksi keuangan dari pesan santai berbahasa Indonesia.

Kategori pengeluaran yang valid: {", ".join(daftar_pengeluaran)}
Sumber pemasukan yang valid: {", ".join(daftar_pemasukan)}
Akun yang valid: {", ".join(daftar_akun)}

Pesan dari user: "{teks}"

Balas HANYA dengan JSON murni (tanpa markdown, tanpa teks lain), format:
{{"kategori": "<salah satu dari daftar di atas, tulis persis sama>", "jumlah": <angka saja tanpa titik/koma>, "akun": "<salah satu dari daftar akun di atas, tulis persis sama>", "deskripsi": "<ringkasan singkat konteks pesan, boleh string kosong>"}}

Kalau kategori, jumlah, atau akun tidak bisa ditentukan dengan yakin dari pesan, balas:
{{"error": "<alasan singkat dalam Bahasa Indonesia>"}}"""

    data = await panggil_ai_json(prompt)
    if not data or "error" in data:
        return None
    return data


def validasi_hasil_ai(data_ai, daftar_pengeluaran, daftar_pemasukan, daftar_akun):
    """Hasil AI tidak langsung dipercaya - tetap divalidasi ke daftar kategori/akun
    asli di sheet, sama seperti jalur parsing format kaku."""
    kategori_mentah = str(data_ai.get("kategori", "")).strip()
    kategori_cocok = cari_kecocokan(kategori_mentah, daftar_pengeluaran)
    if kategori_cocok:
        tipe = "Out"
    else:
        kategori_cocok = cari_kecocokan(kategori_mentah, daftar_pemasukan)
        tipe = "In" if kategori_cocok else None

    if not kategori_cocok:
        return None, f"AI menyebut kategori '{kategori_mentah}' yang tidak ada di daftar kamu."

    akun_mentah = str(data_ai.get("akun", "")).strip()
    akun_cocok = cari_kecocokan(akun_mentah, daftar_akun)
    if not akun_cocok:
        return None, f"AI menyebut akun '{akun_mentah}' yang tidak ada di daftar kamu."

    try:
        jumlah = int(float(data_ai.get("jumlah", 0)))
    except (TypeError, ValueError):
        jumlah = 0
    if jumlah <= 0:
        return None, "AI tidak berhasil menentukan jumlah transaksi dengan jelas."

    deskripsi = str(data_ai.get("deskripsi", "")).strip()

    return {
        "tanggal": tanggal_wib().strftime("%Y-%m-%d"),
        "tipe": tipe,
        "kategori": kategori_cocok,
        "akun": akun_cocok,
        "jumlah": jumlah,
        "deskripsi": deskripsi,
    }, None


async def parse_bulan_dengan_ai(teks, hari_ini):
    """Minta Gemini menentukan bulan & tahun yang dimaksud dari kalimat bebas.
    Hasilnya divalidasi (angka & rentang) sebelum dipakai. Return (bulan, tahun) atau None."""
    if model_ai is None:
        return None
    prompt = f"""Hari ini adalah {hari_ini.isoformat()}. User meminta ringkasan keuangan untuk satu bulan tertentu.
Tentukan bulan dan tahun yang dimaksud dari pesan user berikut.

Pesan user: "{teks}"

Balas HANYA dengan JSON murni (tanpa markdown, tanpa teks lain):
{{"bulan": <angka 1-12>, "tahun": <4 digit>}}

Kalau tidak jelas bulan apa yang dimaksud, jangan menebak, balas:
{{"error": "<alasan singkat>"}}"""

    data = await panggil_ai_json(prompt)
    if not data or "error" in data:
        return None
    try:
        bulan, tahun = int(data.get("bulan")), int(data.get("tahun"))
    except (TypeError, ValueError):
        return None
    return (bulan, tahun) if _bulan_tahun_valid(bulan, tahun) else None


async def deteksi_maksud_dengan_ai(teks, hari_ini):
    """Cadangan terakhir: kalau pesan bukan transaksi, tanya AI apakah user sebenarnya
    meminta ringkasan / saldo / transaksi hari ini. Return dict {'maksud', 'bulan', 'tahun'} atau None."""
    if model_ai is None:
        return None
    prompt = f"""Hari ini adalah {hari_ini.isoformat()}. Kamu membantu bot pencatat keuangan pribadi.
Tentukan apa yang diminta user dari pesan berikut.

Pesan user: "{teks}"

Pilihan maksud:
- "ringkasan": user MEMINTA ringkasan/rekap pemasukan-pengeluaran untuk suatu bulan (tentukan bulan & tahunnya; kalau bulan tidak disebut berarti bulan ini)
- "saldo": user MEMINTA tahu saldo akun/rekeningnya
- "hariini": user MEMINTA melihat transaksi atau pengeluaran hari ini
- "lainnya": selain tiga di atas (termasuk pesan yang sebenarnya mencatat transaksi atau tidak jelas)

Jangan menebak. Kalau ragu, pilih "lainnya".
Balas HANYA dengan JSON murni:
{{"maksud": "ringkasan" atau "saldo" atau "hariini" atau "lainnya", "bulan": <1-12 atau null>, "tahun": <4 digit atau null>}}"""

    data = await panggil_ai_json(prompt)
    if not data:
        return None
    maksud = str(data.get("maksud", "")).strip().lower()
    if maksud not in ("ringkasan", "saldo", "hariini"):
        return None

    hasil = {"maksud": maksud, "bulan": hari_ini.month, "tahun": hari_ini.year}
    if maksud == "ringkasan":
        try:
            bulan, tahun = int(data.get("bulan")), int(data.get("tahun"))
            if _bulan_tahun_valid(bulan, tahun):
                hasil["bulan"], hasil["tahun"] = bulan, tahun
        except (TypeError, ValueError):
            pass  # bulan tidak disebut -> pakai bulan berjalan
    return hasil


def deteksi_maksud_kata_kunci(teks):
    """Kenali permintaan sederhana tanpa AI. Return (maksud, sisa_teks) atau None."""
    t = re.sub(r"[^\w\s/\-]", " ", teks.lower())
    t = re.sub(r"\s+", " ", t).strip()
    kata = t.split()
    if not kata:
        return None

    if kata[0] in ("ringkasan", "rekap", "rekapan", "laporan") and len(kata) <= 8:
        return "ringkasan", " ".join(kata[1:])
    if t in ("saldo", "cek saldo", "lihat saldo", "saldo akun", "saldo semua akun", "sisa saldo"):
        return "saldo", ""
    if t in ("hari ini", "hariini", "transaksi hari ini", "pengeluaran hari ini", "cek hari ini", "lihat hari ini"):
        return "hariini", ""
    return None


# =====================================================================
# Transaksi: parsing format kaku & simpan
# =====================================================================

def cari_kecocokan(kata, daftar):
    # Cocokkan kata (huruf besar/kecil diabaikan) dengan salah satu isi daftar
    kata_lower = kata.lower()
    for item in daftar:
        if item.lower() == kata_lower or item.lower().startswith(kata_lower):
            return item  # kembalikan versi asli dari sheet, misal "Makan" bukan "makan"
    return None


def cari_kategori_di_awal(teks_lower, daftar_kategori):
    """Cari kategori (bisa lebih dari satu kata, mis. 'Belanja Kebutuhan') di awal teks.
    Kategori dengan jumlah kata terbanyak dicek duluan supaya 'Belanja Kebutuhan'
    tidak keburu kecocok sebagai 'Belanja' saja."""
    terurut = sorted(daftar_kategori, key=lambda k: -len(k.split()))
    for kat in terurut:
        kat_lower = kat.lower()
        if teks_lower == kat_lower or teks_lower.startswith(kat_lower + " "):
            return kat
    return None


def parse_pesan(teks):
    """Ubah teks pesan jadi data transaksi. Return (data_dict, None) kalau sukses,
    atau (None, pesan_error) kalau gagal."""
    teks_asli = teks.strip()
    teks_lower = teks_asli.lower()

    # Cocokkan kategori dulu (boleh terdiri dari beberapa kata) - cek pengeluaran, lalu pemasukan
    daftar_pengeluaran = ambil_daftar(ws_kategori, 1)  # kolom A
    daftar_pemasukan = ambil_daftar(ws_kategori, 3)    # kolom C

    kategori_cocok = cari_kategori_di_awal(teks_lower, daftar_pengeluaran)
    if kategori_cocok:
        tipe = "Out"
    else:
        kategori_cocok = cari_kategori_di_awal(teks_lower, daftar_pemasukan)
        tipe = "In" if kategori_cocok else None

    if not kategori_cocok:
        kata_pertama = teks_asli.split(maxsplit=1)[0] if teks_asli else "(kosong)"
        return None, (
            f"Kategori '{kata_pertama}' tidak dikenali.\n"
            f"Kategori pengeluaran: {', '.join(daftar_pengeluaran)}\n"
            f"Sumber pemasukan: {', '.join(daftar_pemasukan)}"
        )

    # Sisa teks setelah kategori = jumlah, akun, deskripsi
    sisa_teks = teks_asli[len(kategori_cocok):].strip()
    bagian = sisa_teks.split(maxsplit=2)
    if len(bagian) < 2:
        return None, (
            "Format pesan kurang lengkap.\n"
            "Contoh: makan 15000 bca beli nasi goreng"
        )

    jumlah_input, akun_input = bagian[0], bagian[1]
    deskripsi = bagian[2] if len(bagian) > 2 else ""

    jumlah_bersih = jumlah_input.replace(".", "").replace(",", "")
    if not jumlah_bersih.isdigit():
        return None, f"'{jumlah_input}' bukan angka yang valid untuk jumlah."
    jumlah = int(jumlah_bersih)

    daftar_akun = ambil_daftar(ws_akun, 1)  # kolom A
    akun_cocok = cari_kecocokan(akun_input, daftar_akun)
    if not akun_cocok:
        return None, f"Akun '{akun_input}' tidak dikenali. Akun tersedia: {', '.join(daftar_akun)}"

    return {
        "tanggal": tanggal_wib().strftime("%Y-%m-%d"),
        "tipe": tipe,
        "kategori": kategori_cocok,
        "akun": akun_cocok,
        "jumlah": jumlah,
        "deskripsi": deskripsi,
    }, None


def simpan_ke_sheet(data):
    kolom_a = ws_transaksi.col_values(1)
    baris_baru = len(kolom_a) + 1

    ws_transaksi.update_cell(baris_baru, 1, data["tanggal"])
    ws_transaksi.update_cell(baris_baru, 3, data["tipe"])
    ws_transaksi.update_cell(baris_baru, 4, data["kategori"])
    ws_transaksi.update_cell(baris_baru, 5, data["akun"])
    ws_transaksi.update_cell(baris_baru, 6, data["jumlah"])
    ws_transaksi.update_cell(baris_baru, 7, data["deskripsi"])


# =====================================================================
# Pengirim balasan (dipakai bersama oleh command & pesan natural)
# =====================================================================

async def kirim_saldo(update: Update):
    try:
        nama_list = ws_akun.col_values(1)[4:]  # mulai baris 5
        saldo_list = ws_akun.col_values(3, value_render_option="UNFORMATTED_VALUE")[4:]
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal ambil data saldo: {e}")
        return

    baris = []
    total = 0
    for nama, saldo in zip(nama_list, saldo_list):
        if not nama.strip():
            continue
        nilai = saldo if isinstance(saldo, (int, float)) else 0
        total += nilai
        baris.append(f"• {nama}: {format_rp(nilai)}")

    if not baris:
        await update.message.reply_text("Belum ada akun yang terdaftar di sheet Akun.")
        return

    pesan = "💰 Saldo Akun:\n" + "\n".join(baris) + f"\n\nTotal: {format_rp(total)}"
    await update.message.reply_text(pesan)


async def kirim_ringkasan(update: Update, bulan, tahun):
    try:
        transaksi = await asyncio.to_thread(ambil_transaksi)
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal ambil data transaksi: {e}")
        return
    await update.message.reply_text(format_ringkasan(bulan, tahun, filter_bulan(transaksi, bulan, tahun)))


async def kirim_hariini(update: Update):
    try:
        transaksi = await asyncio.to_thread(ambil_transaksi)
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal ambil data transaksi: {e}")
        return

    hari_ini = tanggal_wib()
    label_tanggal = hari_ini.strftime("%d/%m/%Y")
    hari_ini_list = [t for t in transaksi if t["tanggal"] == hari_ini]
    if not hari_ini_list:
        await update.message.reply_text(f"Belum ada transaksi tercatat hari ini ({label_tanggal}).")
        return

    baris_hasil = []
    total_in = 0
    total_out = 0
    for t in hari_ini_list:
        tanda = "-" if t["tipe"] == "Out" else "+"
        baris = f"{tanda}{format_rp(t['jumlah'])} | {t['kategori']} ({t['akun']})"
        if t["deskripsi"]:
            baris += f" - {t['deskripsi']}"
        baris_hasil.append(baris)
        if t["tipe"] == "Out":
            total_out += t["jumlah"]
        else:
            total_in += t["jumlah"]

    pesan = f"🗓️ Transaksi Hari Ini ({label_tanggal}):\n\n" + "\n".join(baris_hasil)
    pesan += f"\n\nTotal masuk: {format_rp(total_in)}\nTotal keluar: {format_rp(total_out)}"
    await update.message.reply_text(pesan)


MENUNGGU_BULAN_MAKS_DETIK = 300  # jawaban "bulan apa?" berlaku 5 menit


async def tanya_bulan(update: Update, context: ContextTypes.DEFAULT_TYPE, awalan=""):
    context.user_data["menunggu_bulan"] = time.time()
    await update.message.reply_text(
        f"{awalan}Mau menampilkan ringkasan bulan apa?\n"
        "Contoh: agustus, bulan lalu, 2 bulan lalu, atau 08/2026"
    )


async def tampilkan_ringkasan_dari_frasa(update: Update, frasa):
    """Pahami bulan dari frasa (kata kunci dulu, AI sebagai cadangan), lalu kirim ringkasannya.
    Return True kalau berhasil."""
    hari_ini = tanggal_wib()
    hasil = parse_bulan(frasa, hari_ini)
    if hasil is None:
        hasil = await parse_bulan_dengan_ai(frasa, hari_ini)
    if hasil is None:
        return False
    await kirim_ringkasan(update, hasil[0], hasil[1])
    return True


async def tangani_jawaban_bulan(update: Update, context: ContextTypes.DEFAULT_TYPE, teks):
    """Kalau bot baru saja bertanya 'bulan apa?', pesan ini dianggap jawabannya.
    Return True kalau pesan sudah ditangani di sini."""
    mulai = context.user_data.get("menunggu_bulan")
    if not mulai:
        return False
    context.user_data.pop("menunggu_bulan", None)  # satu kali jawab; gagal -> user ulangi perintahnya
    if time.time() - mulai > MENUNGGU_BULAN_MAKS_DETIK:
        return False

    if await tampilkan_ringkasan_dari_frasa(update, teks):
        return True

    # Bukan bulan. Mungkin user justru mengirim transaksi biasa - biarkan diproses normal.
    _, error = parse_pesan(teks)
    if error is None:
        return False

    await update.message.reply_text(
        "Maaf, saya belum menangkap bulan yang dimaksud. "
        "Ketik /ringkasanbulanan untuk mencoba lagi, atau tulis langsung misalnya "
        "\"/ringkasanbulanan agustus\"."
    )
    return True


# =====================================================================
# Pengaman akses: hanya pemilik yang boleh memakai bot
# =====================================================================

async def gerbang_akses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dijalankan paling awal untuk SETIAP update. Selain pemilik -> ditolak, tidak diteruskan
    ke handler lain. Satu-satunya pengecualian: /id (hanya membalas ID si pengirim sendiri)."""
    pesan = update.effective_message
    user = update.effective_user

    teks = pesan.text if pesan is not None and pesan.text else ""
    perintah = teks.split()[0].split("@")[0].lower() if teks.strip() else ""
    if perintah == "/id":
        return

    if user is not None and ALLOWED_USER_ID is not None and user.id == ALLOWED_USER_ID:
        return

    nama = user.username if user is not None and user.username else "-"
    uid = user.id if user is not None else "-"
    print(f"[AKSES DITOLAK] user_id={uid} username={nama}")

    if pesan is not None:
        try:
            if ALLOWED_USER_ID is None:
                await pesan.reply_text(
                    "Bot belum dikonfigurasi. Kirim /id untuk melihat Telegram ID kamu, "
                    "lalu isi variable ALLOWED_USER_ID di server."
                )
            else:
                await pesan.reply_text("Maaf, bot ini bersifat pribadi dan hanya melayani pemiliknya. 🔒")
        except Exception as e:
            print(f"[AKSES DITOLAK] gagal membalas: {type(e).__name__}: {e}")
    raise ApplicationHandlerStop


# =====================================================================
# Handler command
# =====================================================================

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Telegram ID kamu: {update.effective_user.id}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("menunggu_bulan", None)
    pesan = (
        "Halo! Kirim transaksi dengan format:\n"
        "<kategori> <jumlah> <akun> <deskripsi opsional>\n\n"
        "Contoh: makan 15000 bca beli nasi goreng\n\n"
        "Command lain:\n"
        "/saldo - cek saldo semua akun\n"
        "/ringkasan - rekap bulan berjalan\n"
        "/ringkasanbulanan - rekap bulan apa saja\n"
        "/hariini - transaksi hari ini\n"
        f"\nBot akan memberi peringatan kalau pengeluaran harian melewati {format_rp(BATAS_HARIAN)}."
    )
    if model_ai is not None:
        pesan += (
            "\n\nAtau tulis bebas juga bisa, contoh:\n"
            "\"td abis makan siang di warung 25rb pake bca\" 🤖\n"
            "\"ringkasan agustus\" atau \"pengeluaran bulan lalu gimana?\""
        )
    await update.message.reply_text(pesan)


async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("menunggu_bulan", None)
    await kirim_saldo(update)


async def cmd_hariini(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("menunggu_bulan", None)
    await kirim_hariini(update)


async def cmd_ringkasan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ringkasan SELALU menampilkan bulan berjalan."""
    context.user_data.pop("menunggu_bulan", None)
    hari_ini = tanggal_wib()
    await kirim_ringkasan(update, hari_ini.month, hari_ini.year)


async def cmd_ringkasanbulanan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ringkasanbulanan -> bot bertanya bulan apa. Boleh juga langsung: /ringkasanbulanan agustus"""
    context.user_data.pop("menunggu_bulan", None)
    frasa = " ".join(context.args) if context.args else ""
    if not frasa.strip():
        await tanya_bulan(update, context)
        return
    if not await tampilkan_ringkasan_dari_frasa(update, frasa):
        await tanya_bulan(update, context, awalan="Maaf, saya belum menangkap bulan yang dimaksud. ")


# =====================================================================
# Handler pesan biasa (transaksi / permintaan dalam bahasa natural)
# =====================================================================

async def catat_transaksi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teks = update.message.text

    # 0. Bot baru saja bertanya "bulan apa?" -> pesan ini kemungkinan jawabannya
    if await tangani_jawaban_bulan(update, context, teks):
        return

    # 1. Format kaku dulu - instan & gratis, tidak pakai kuota AI
    data, error = parse_pesan(teks)
    dibantu_ai = False

    # 2. Bukan transaksi? Kenali permintaan sederhana lewat kata kunci (tanpa AI)
    if error:
        maksud = deteksi_maksud_kata_kunci(teks)
        if maksud is not None:
            jenis, sisa = maksud
            if jenis == "saldo":
                await kirim_saldo(update)
            elif jenis == "hariini":
                await kirim_hariini(update)
            else:  # ringkasan
                if not sisa:
                    hari_ini = tanggal_wib()
                    await kirim_ringkasan(update, hari_ini.month, hari_ini.year)
                elif not await tampilkan_ringkasan_dari_frasa(update, sisa):
                    await tanya_bulan(update, context, awalan="Maaf, saya belum menangkap bulan yang dimaksud. ")
            return

    # 3. Masih gagal -> coba pahami sebagai transaksi bahasa natural lewat AI
    if error and model_ai is not None:
        daftar_pengeluaran = ambil_daftar(ws_kategori, 1)
        daftar_pemasukan = ambil_daftar(ws_kategori, 3)
        daftar_akun = ambil_daftar(ws_akun, 1)

        data_ai = await parse_dengan_ai(teks, daftar_pengeluaran, daftar_pemasukan, daftar_akun)
        if data_ai:
            data_valid, error_ai = validasi_hasil_ai(data_ai, daftar_pengeluaran, daftar_pemasukan, daftar_akun)
            if data_valid:
                data, error = data_valid, None
                dibantu_ai = True
            else:
                error = error_ai

    # 4. Terakhir: mungkin ini permintaan (ringkasan/saldo/hari ini) dengan kalimat bebas
    if error and model_ai is not None:
        maksud_ai = await deteksi_maksud_dengan_ai(teks, tanggal_wib())
        if maksud_ai is not None:
            if maksud_ai["maksud"] == "saldo":
                await kirim_saldo(update)
            elif maksud_ai["maksud"] == "hariini":
                await kirim_hariini(update)
            else:
                await kirim_ringkasan(update, maksud_ai["bulan"], maksud_ai["tahun"])
            return

    if error:
        await update.message.reply_text(f"⚠️ {error}")
        return

    try:
        simpan_ke_sheet(data)
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal simpan ke sheet: {e}")
        return

    tanda = "-" if data["tipe"] == "Out" else "+"
    prefiks = "🤖 " if dibantu_ai else "✅ "
    await update.message.reply_text(
        f"{prefiks}Tercatat: {data['kategori']} {tanda}Rp{data['jumlah']:,} ({data['akun']})"
    )

    # Peringatan batas pengeluaran harian (transfer antar akun tidak dihitung)
    if data["tipe"] == "Out" and not adalah_transfer(data["kategori"]):
        try:
            transaksi = await asyncio.to_thread(ambil_transaksi)
            tanggal_transaksi = date.fromisoformat(data["tanggal"])
            total = hitung_pengeluaran_harian(transaksi, tanggal_transaksi)
            peringatan = buat_peringatan_batas(total, data["jumlah"], BATAS_HARIAN)
            if peringatan:
                await update.message.reply_text(peringatan)
        except Exception as e:
            print(f"[BATAS] Gagal cek batas harian: {type(e).__name__}: {e}")


async def tangani_error(update, context: ContextTypes.DEFAULT_TYPE):
    """Semua error tak terduga dicatat ke log, dan pemilik diberi tahu singkat."""
    print(f"[ERROR] {type(context.error).__name__}: {context.error}")
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text("❌ Terjadi kesalahan di bot. Coba lagi sebentar lagi.")
        except Exception:
            pass


def buat_aplikasi():
    app = Application.builder().token(TOKEN).build()

    # Gerbang akses: grup -1 = dijalankan lebih dulu daripada semua handler lain
    app.add_handler(TypeHandler(Update, gerbang_akses), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("saldo", cmd_saldo))
    app.add_handler(CommandHandler("ringkasan", cmd_ringkasan))
    app.add_handler(CommandHandler("ringkasanbulanan", cmd_ringkasanbulanan))
    app.add_handler(CommandHandler("hariini", cmd_hariini))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE, catat_transaksi))
    app.add_error_handler(tangani_error)
    return app


def main():
    if ALLOWED_USER_ID is None:
        print("[PERINGATAN] ALLOWED_USER_ID belum diisi - bot menolak semua orang. "
              "Kirim /id ke bot untuk mendapatkan ID kamu, lalu isi variable ALLOWED_USER_ID.")
    app = buat_aplikasi()
    print("Bot berjalan... tekan Ctrl+C untuk berhenti.")
    app.run_polling()


if __name__ == "__main__":
    main()
