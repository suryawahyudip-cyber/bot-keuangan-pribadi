# bot.py (v3)
# Sekarang pakai environment variables (bukan config.py) supaya aman untuk di-deploy.
# Format pesan: <kategori> <jumlah> <akun> <deskripsi opsional>
# Contoh: "makan 15000 bca beli nasi goreng"

import os
import json
import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
import google.generativeai as genai
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv

load_dotenv()  # baca file .env kalau ada (untuk development di laptop)

WIB = ZoneInfo("Asia/Jakarta")


def tanggal_wib():
    """Tanggal 'hari ini' menurut waktu Indonesia (WIB), bukan waktu server.
    Server (Railway) biasanya pakai UTC, yang bisa beda hari dengan WIB
    terutama dini hari (00:00-06:59 WIB = masih hari sebelumnya di UTC)."""
    return datetime.now(WIB).date()


TOKEN = os.environ["TELEGRAM_TOKEN"]
SHEET_ID = os.environ["SHEET_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")  # opsional - tanpa ini, bot tetap jalan pakai format kaku saja

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model_ai = genai.GenerativeModel("gemini-2.5-flash-lite")
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
ws_ringkasan = sheet.worksheet("Ringkasan Bulanan")


def ambil_daftar(worksheet, kolom):
    # Ambil semua nilai di satu kolom (mulai baris 5), buang sel kosong
    nilai_mentah = worksheet.col_values(kolom)[4:]  # index 4 = baris 5 (mulai dari 0)
    return [v for v in nilai_mentah if v.strip() != ""]


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


def format_rp(angka):
    try:
        angka = float(angka)
    except (TypeError, ValueError):
        angka = 0
    tanda = "-" if angka < 0 else ""
    return f"{tanda}Rp{abs(angka):,.0f}"


# --- Handler bot Telegram ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pesan = (
        "Halo! Kirim transaksi dengan format:\n"
        "<kategori> <jumlah> <akun> <deskripsi opsional>\n\n"
        "Contoh: makan 15000 bca beli nasi goreng\n\n"
        "Command lain:\n"
        "/saldo - cek saldo semua akun\n"
        "/ringkasan - rekap bulan ini\n"
        "/hariini - transaksi hari ini"
    )
    if model_ai is not None:
        pesan += (
            "\n\nAtau tulis bebas juga bisa, contoh:\n"
            "\"td abis makan siang di warung 25rb pake bca\" 🤖"
        )
    await update.message.reply_text(pesan)


async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def cmd_ringkasan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    baris_bulan_ini = 5  # baris pertama di Ringkasan Bulanan selalu bulan berjalan (formula pakai TODAY())
    try:
        bulan_label = ws_ringkasan.cell(baris_bulan_ini, 1).value
        nilai = ws_ringkasan.row_values(baris_bulan_ini, value_render_option="UNFORMATTED_VALUE")
        headers = ws_ringkasan.row_values(4)
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal ambil ringkasan: {e}")
        return

    pemasukan = nilai[1] if len(nilai) > 1 else 0
    pengeluaran = nilai[2] if len(nilai) > 2 else 0
    saldo_bersih = nilai[3] if len(nilai) > 3 else 0

    baris_kategori = []
    for i in range(4, len(headers)):
        nama_kategori = headers[i]
        nilai_kategori = nilai[i] if i < len(nilai) else 0
        if isinstance(nilai_kategori, (int, float)) and nilai_kategori:
            baris_kategori.append(f"• {nama_kategori}: {format_rp(nilai_kategori)}")

    pesan = (
        f"📊 Ringkasan {bulan_label}\n\n"
        f"Pemasukan: {format_rp(pemasukan)}\n"
        f"Pengeluaran: {format_rp(pengeluaran)}\n"
        f"Saldo Bersih: {format_rp(saldo_bersih)}"
    )
    if baris_kategori:
        pesan += "\n\nPer Kategori:\n" + "\n".join(baris_kategori)

    await update.message.reply_text(pesan)


async def parse_dengan_ai(teks, daftar_pengeluaran, daftar_pemasukan, daftar_akun):
    """Minta Gemini mengekstrak data transaksi dari pesan bebas (bahasa natural).
    Return dict mentah dari AI, atau None kalau gagal/AI tidak tersedia."""
    if model_ai is None:
        return None

    prompt = f"""Kamu mengekstrak data transaksi keuangan dari pesan santai berbahasa Indonesia.

Kategori pengeluaran yang valid: {", ".join(daftar_pengeluaran)}
Sumber pemasukan yang valid: {", ".join(daftar_pemasukan)}
Akun yang valid: {", ".join(daftar_akun)}

Pesan dari user: "{teks}"

Balas HANYA dengan JSON murni (tanpa markdown, tanpa teks lain), format:
{{"kategori": "<salah satu dari daftar di atas, tulis persis sama>", "jumlah": <angka saja tanpa titik/koma>, "akun": "<salah satu dari daftar akun di atas, tulis persis sama>", "deskripsi": "<ringkasan singkat konteks pesan, boleh string kosong>"}}

Kalau kategori, jumlah, atau akun tidak bisa ditentukan dengan yakin dari pesan, balas:
{{"error": "<alasan singkat dalam Bahasa Indonesia>"}}"""

    try:
        respons = await asyncio.to_thread(model_ai.generate_content, prompt)
        teks_respons = respons.text.strip()
        teks_respons = teks_respons.replace("```json", "").replace("```", "").strip()
        data = json.loads(teks_respons)
    except Exception:
        return None

    if not isinstance(data, dict) or "error" in data:
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


def serial_ke_tanggal(nilai):
    """Google Sheets menyimpan tanggal sebagai 'serial number' (hari sejak 30 Des 1899).
    Fungsi ini mengubahnya balik jadi objek date Python, apa pun bentuk nilainya."""
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


async def cmd_hariini(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        kolom_tanggal = ws_transaksi.col_values(1, value_render_option="UNFORMATTED_VALUE")[4:]
        kolom_tipe = ws_transaksi.col_values(3)[4:]
        kolom_kategori = ws_transaksi.col_values(4)[4:]
        kolom_akun = ws_transaksi.col_values(5)[4:]
        kolom_jumlah = ws_transaksi.col_values(6, value_render_option="UNFORMATTED_VALUE")[4:]
        kolom_deskripsi = ws_transaksi.col_values(7)[4:]
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal ambil data transaksi: {e}")
        return

    hari_ini = tanggal_wib()
    baris_hasil = []
    total_in = 0
    total_out = 0

    jumlah_baris = len(kolom_tanggal)
    for i in range(jumlah_baris):
        tanggal = serial_ke_tanggal(kolom_tanggal[i])
        if tanggal != hari_ini:
            continue

        tipe = kolom_tipe[i] if i < len(kolom_tipe) else ""
        kategori = kolom_kategori[i] if i < len(kolom_kategori) else ""
        akun = kolom_akun[i] if i < len(kolom_akun) else ""
        jumlah = kolom_jumlah[i] if i < len(kolom_jumlah) else 0
        deskripsi = kolom_deskripsi[i] if i < len(kolom_deskripsi) else ""

        jumlah_num = jumlah if isinstance(jumlah, (int, float)) else 0
        tanda = "-" if tipe == "Out" else "+"
        baris = f"{tanda}{format_rp(jumlah_num)} | {kategori} ({akun})"
        if deskripsi:
            baris += f" - {deskripsi}"
        baris_hasil.append(baris)

        if tipe == "Out":
            total_out += jumlah_num
        else:
            total_in += jumlah_num

    label_tanggal = hari_ini.strftime("%d/%m/%Y")
    if not baris_hasil:
        await update.message.reply_text(f"Belum ada transaksi tercatat hari ini ({label_tanggal}).")
        return

    pesan = f"🗓️ Transaksi Hari Ini ({label_tanggal}):\n\n" + "\n".join(baris_hasil)
    pesan += f"\n\nTotal masuk: {format_rp(total_in)}\nTotal keluar: {format_rp(total_out)}"
    await update.message.reply_text(pesan)


async def catat_transaksi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teks = update.message.text
    data, error = parse_pesan(teks)  # coba format kaku dulu - instan & gratis, tidak pakai kuota AI
    dibantu_ai = False

    if error and model_ai is not None:
        # format kaku gagal - coba pesan natural lewat AI sebagai fallback
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


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("saldo", cmd_saldo))
    app.add_handler(CommandHandler("ringkasan", cmd_ringkasan))
    app.add_handler(CommandHandler("hariini", cmd_hariini))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, catat_transaksi))

    print("Bot berjalan... tekan Ctrl+C untuk berhenti.")
    app.run_polling()


if __name__ == "__main__":
    main()
