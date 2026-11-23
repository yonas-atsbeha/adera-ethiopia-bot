import math
import random
import sqlite3
from datetime import datetime, timedelta
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ==========================================================
# 1. ማስተካከያዎች (Configuration)
# ==========================================================
BOT_TOKEN = "9137375988:AAFVbMj815-8nBeuNTBSiDV9v8QOdn3RJGA"

# የመስራቾች/አድሚኖች Telegram User IDs (ከ @userinfobot ያገኙትን ቁጥር እዚህ ያስገቡ)
ADMIN_IDS = [5823203935]

# የሀገር ውስጥ የክፍያ መቀበያ አካውንቶች
PAYMENT_ACCOUNTS = (
    "• **Telebirr:** `09XXXXXXXX` (ADERA Ethiopia)\n"
    "• **CBE (ንግድ ባንክ):** `1000XXXXXXXXX` (ADERA Ethiopia)\n"
    "• **CBE Birr / Abyssinia:** `09XXXXXXXX`"
)

COMMISSION_RATE = 0.12       # 12% የአደራ ኮሚሽን
INSURANCE_RATE = 0.03        # 3% የእቃ ዋጋ ኢንሹራንስ
AIRPORT_SURCHARGE = 150.0    # ለኤርፖርት / ተጨማሪ ርቀት ማካካሻ

RATE_PER_KG = {
    "አዲስ አበባ ➔ አዳማ": 120.0,
    "አዲስ አበባ ➔ ሀዋሳ": 200.0,
    "አዲስ አበባ ➔ ባህር ዳር": 250.0,
    "አዲስ አበባ ➔ ድሬዳዋ": 250.0,
}

# የውይይት ደረጃዎች (States)
(
    MAIN_MENU,
    # Sender States
    S_ACCOUNT_TYPE,
    S_ROUTE,
    S_PICKUP_TYPE,
    S_PICKUP_DETAIL,
    S_DROPOFF_TYPE,
    S_DROPOFF_DETAIL,
    S_RECEIVER_INFO,
    S_DELIVERY_DAY,
    S_CATEGORY,
    S_WEIGHT,
    S_ITEM_DESC,
    S_OFFER_PRICE,
    S_INSURANCE_CHOICE,
    S_ITEM_VALUE,
    S_PAYMENT_PROOF,
    # Traveler States
    T_PHONE,
    T_FAYDA,
    T_TERMS,
    T_ACTION,
    T_ROUTE,
    T_DAY,
    T_WINDOW,
    T_CAPACITY,
    # Other Actions
    PICKUP_MEDIA_PROOF,
    COUNTER_OFFER_INPUT,
    RELAY_CHAT_INPUT,
    VERIFY_PIN,
    TRACK_ORDER,
    DISPUTE_ORDER_ID,
    DISPUTE_REASON,
) = range(31)


# ==========================================================
# 2. የዳታቤዝ ማዋቀሪያ (SQLite)
# ==========================================================
def init_db():
    conn = sqlite3.connect("adera_logistics.db")
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            role TEXT,
            account_type TEXT DEFAULT 'INDIVIDUAL',
            phone TEXT,
            fayda_photo_id TEXT,
            terms_accepted INTEGER DEFAULT 0,
            rating_sum REAL DEFAULT 0,
            rating_count INTEGER DEFAULT 0
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS trips (
            trip_id INTEGER PRIMARY KEY AUTOINCREMENT,
            traveler_id INTEGER,
            route TEXT,
            travel_day TEXT,
            time_window TEXT,
            total_capacity_kg REAL,
            remaining_capacity_kg REAL,
            status TEXT DEFAULT 'ACTIVE'
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS shipments (
            shipment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER,
            traveler_id INTEGER,
            sender_type TEXT,
            route TEXT,
            pickup_point TEXT,
            dropoff_point TEXT,
            receiver_info TEXT,
            delivery_day TEXT,
            expires_at TEXT,
            category TEXT,
            actual_weight REAL,
            billable_weight REAL,
            item_desc TEXT,
            min_price REAL,
            delivery_fee REAL,
            commission_amt REAL,
            insurance_amt REAL,
            traveler_payout REAL,
            total_escrow REAL,
            payment_proof TEXT,
            pickup_media_type TEXT,
            pickup_media_id TEXT,
            delivery_pin TEXT,
            status TEXT DEFAULT 'OPEN_FOR_TRAVELERS'
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS disputes (
            dispute_id INTEGER PRIMARY KEY AUTOINCREMENT,
            shipment_id INTEGER,
            reporter_id INTEGER,
            reason TEXT,
            created_at TEXT
        )"""
    )
    conn.commit()
    conn.close()


# ==========================================================
# 3. ረዳት ተግባራት (Helpers)
# ==========================================================
def calculate_billable_weight(actual_kg: float) -> float:
    if actual_kg <= 1.0:
        return 1.0
    return float(max(1, math.ceil(actual_kg - 0.3)))


def calculate_min_price(route: str, category: str, billable_kg: float, is_airport: bool) -> float:
    base_rate = RATE_PER_KG.get(route, 200.0)
    if "ሰነድ" in category:
        price = max(200.0, base_rate)
    elif "ኤሌክትሮኒክስ" in category:
        price = max(800.0, (base_rate * billable_kg) + 500.0)
    else:
        price = base_rate * billable_kg

    if is_airport:
        price += AIRPORT_SURCHARGE
    return round(price, 2)


def recompute_pricing(fee: float, insurance_amt: float):
    commission = round(fee * COMMISSION_RATE, 2)
    traveler_payout = round(fee - commission, 2)
    total_escrow = round(fee + insurance_amt, 2)
    return commission, traveler_payout, total_escrow


def get_persistent_menu():
    keyboard = [
        ["📦 እቃ መላክ (Send)", "🚌 ተጓዥ ነኝ (Traveler)"],
        ["✈️ ጉዞዎችን ማየት (Trips)", "📍 መከታተል (Track)"],
        ["📸/🎥 ርክክብ ፎቶ/ቪዲዮ", "🔑 ኮድ ማስገባት (PIN)"],
        ["💬 ለአጋር መልዕክት (Chat)", "🚫 ሰርዝ / ገንዘብ ተመላሽ"],
        ["⚠️ ችግር ሪፖርት (Dispute)", "🏠 ዋና ማውጫ (Menu)"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ==========================================================
# 4. ዋና ማውጫ እና መስተንግዶ (Start & Menu Router)
# ==========================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    welcome_text = (
        f"እንኳን ወደ **አደራ (ADERA Ethiopia)** በደህና መጡ {user.first_name}! 🇪🇹\n\n"
        "የከተማ-ለከተማ Peer-to-Peer (P2P) የእቃ ማጓጓዣ መድረክ።\n"
        "✅ በፋይዳ (Fayda) መታወቂያ የተረጋገጡ ተጓዦች (እስከ 20 ኪ.ግ)\n"
        "✅ ተጓዥ ሲስማማ የሚከፈል ኢስክሮ (Escrow) በ Telebirr እና ባንክ\n"
        "✅ የፎቶ/ቪዲዮ ማረጋገጫ፣ PIN ርክክብ፣ ስረዛ/ተመላሽ እና ደረጃ አሰጣጥ\n\n"
        "👇 ከታች ካለው **ቋሚ ሜኑ** አገልግሎት ይምረጡ (/start መጻፍ አያስፈልግዎትም)፡"
    )
    await update.message.reply_text(welcome_text, reply_markup=get_persistent_menu(), parse_mode="Markdown")
    return MAIN_MENU


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if "ዋና ማውጫ" in text or "Menu" in text:
        return await start(update, context)

    elif "እቃ መላክ" in text or "Send" in text:
        kb = [["👤 ግለሰብ ላኪ (Individual)"], ["🏪 ቋሚ ነጋዴ / ሱቅ (B2B Merchant)"]]
        await update.message.reply_text(
            "እቃ የሚልኩት እንደ ግለሰብ ነው ወይስ እንደ ቋሚ ነጋዴ?",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
        )
        return S_ACCOUNT_TYPE

    elif "ተጓዥ ነኝ" in text or "Traveler" in text:
        await update.message.reply_text(
            "🚌 **የተጓዥ ምዝገባ**\nየቴሌብር (Telebirr) ስልክ ቁጥርዎን ያስገቡ (ለምሳሌ፡ `0911223344`)፡",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
        return T_PHONE

    elif "ጉዞዎችን ማየት" in text or "Trips" in text:
        return await show_active_trips(update, context)

    elif "መከታተል" in text or "Track" in text:
        await update.message.reply_text(
            "📍 የትዕዛዝ ቁጥሩን ያስገቡ (ለምሳሌ፡ `1` ወይም `ADR-1`)፡",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
        return TRACK_ORDER

    elif "ፎቶ/ቪዲዮ" in text:
        await update.message.reply_text(
            "📸🎥 እቃውን ሲረከቡ ከፍተው ካዩ በኋላ፣ **የትዕዛዝ ቁጥሩን** (ለምሳሌ `ADR-1`) በCaption ላይ ጽፈው **ፎቶ ወይም አጭር ቪዲዮ** ይላኩ፡",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
        return PICKUP_MEDIA_PROOF

    elif "ኮድ ማስገባት" in text or "PIN" in text:
        await update.message.reply_text(
            "🔑 እቃው በትክክል ሲደርስ ተቀባዩ የሚሰጥዎትን **4-አሃዝ ሚስጥራዊ ኮድ (PIN)** ያስገቡ፡",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
        return VERIFY_PIN

    elif "መልዕክት" in text or "Chat" in text:
        await update.message.reply_text(
            "💬 ለላኪው ወይም ለተጓዡ መልዕክት ለመላክ በዚህ ቅርጸት ይጻፉ：\n"
            "`ADR-1: ቦሌ ኤርፖርት ተርሚናል 1 በር ላይ ደርሻለሁ`",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
        return RELAY_CHAT_INPUT

    elif "ሰርዝ" in text or "ተመላሽ" in text:
        await update.message.reply_text(
            "🚫 ትዕዛዝ ለመሰረዝ ወይም የገንዘብ ተመላሽ (`REFUND`) ለመጠየቅ የትዕዛዝ ቁጥሩን ያስገቡ (ለምሳሌ `ADR-1`)፡",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
        return DISPUTE_ORDER_ID

    elif "ችግር ሪፖርት" in text or "Dispute" in text:
        await update.message.reply_text(
            "⚠️ ችግር የገጠመዎትን የትዕዛዝ ቁጥር ያስገቡ (ለምሳሌ፡ `ADR-1`)፡",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
        return DISPUTE_ORDER_ID

    return await start(update, context)


# ==========================================================
# 5. የላኪ ሂደት (SENDER FLOW - WITH RECEIVER & ESCROW AFTER MATCH)
# ==========================================================
async def s_account_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["sender_type"] = "MERCHANT" if "ነጋዴ" in update.message.text else "INDIVIDUAL"
    routes = [
        ["አዲስ አበባ ➔ አዳማ", "አዲስ አበባ ➔ ሀዋሳ"],
        ["አዲስ አበባ ➔ ባህር ዳር", "አዲስ አበባ ➔ ድሬዳዋ"],
    ]
    await update.message.reply_text(
        "መስመር ይምረጡ፡",
        reply_markup=ReplyKeyboardMarkup(routes, resize_keyboard=True, one_time_keyboard=True),
    )
    return S_ROUTE


async def s_route(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["route"] = update.message.text.strip()
    kb = [["🏙️ ከተማ ውስጥ / መርካቶ / ንግድ ማዕከል"], ["✈️ ቦሌ ኤርፖርት (+150 ብር ማካካሻ)"]]
    await update.message.reply_text(
        "እቃው የሚነሳበት (Pickup) አካባቢ ምን አይነት ነው?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
    )
    return S_PICKUP_TYPE


async def s_pickup_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["is_airport_pickup"] = "ኤርፖርት" in update.message.text
    await update.message.reply_text(
        "የመረከቢያ ትክክለኛ ቦታ (Landmark) ይጻፉ (ለምሳሌ፡ **መርካቶ ጣና ገበያ** ወይም **ቦሌ ኤርፖርት**)፡",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown",
    )
    return S_PICKUP_DETAIL


async def s_pickup_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["pickup_point"] = update.message.text.strip()
    kb = [["🏙️ ከተማ መሃል / መናኸሪያ"], ["✈️ መድረሻ ኤርፖርት (+150 ብር ማካካሻ)"]]
    await update.message.reply_text(
        "እቃው የሚደርስበት (Drop-off) ቦታ ምን አይነት ነው?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
    )
    return S_DROPOFF_TYPE


async def s_dropoff_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["is_airport_dropoff"] = "ኤርፖርት" in update.message.text
    await update.message.reply_text(
        "የማስረከቢያ ትክክለኛ ቦታ ይጻፉ (ለምሳሌ፡ **ባህር ዳር ኤርፖርት** ወይም **አዳማ መሃል**)፡",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown",
    )
    return S_DROPOFF_DETAIL


async def s_dropoff_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["dropoff_point"] = update.message.text.strip()
    await update.message.reply_text(
        "መድረሻ ከተማ ላይ እቃውን የሚረከበውን ሰው **ስም እና ስልክ ቁጥር** ያስገቡ\n"
        "*(ለምሳሌ፡ `አበበ በቀለ - 0911001122`)*፡",
        parse_mode="Markdown",
    )
    return S_RECEIVER_INFO


async def s_receiver_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["receiver_info"] = update.message.text.strip()
    days = [["ዛሬ (Today)", "ነገ (Tomorrow)"], ["ሰኞ (Monday)", "በ3 ቀን ውስጥ (Flexible)"]]
    await update.message.reply_text(
        "እቃው መቼ እንዲደርስ ይፈልጋሉ? (ቀንና በረራ/ሰዓት ይምረጡ ወይም ይጻፉ)፡",
        reply_markup=ReplyKeyboardMarkup(days, resize_keyboard=True, one_time_keyboard=True),
    )
    return S_DELIVERY_DAY


async def s_delivery_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["delivery_day"] = update.message.text.strip()
    cats = [
        ["📄 ሰነድ / ደብዳቤ (Documents)"],
        ["👕 ልብስ / ጫማ / መደበኛ እቃ (Standard)"],
        ["💻 ኤሌክትሮኒክስ (Laptop / Phone / Camera)"],
    ]
    await update.message.reply_text(
        "የእቃውን ምድብ (Category) ይምረጡ፡",
        reply_markup=ReplyKeyboardMarkup(cats, resize_keyboard=True, one_time_keyboard=True),
    )
    return S_CATEGORY


async def s_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["category"] = update.message.text.strip()
    await update.message.reply_text(
        "የእቃውን ክብደት በኪሎ ግራም (kg) ያስገቡ (ከ `0.1` እስከ `20` ኪ.ግ፤ ለምሳሌ `1.5`)：\n"
        "*(ከ 1 ኪ.ግ በታች ➔ 1 ኪ.ግ | ከ 1.3 ኪ.ግ በላይ ➔ 2 ኪ.ግ)*",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown",
    )
    return S_WEIGHT


async def s_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        actual_kg = float(update.message.text.strip())
        if actual_kg <= 0 or actual_kg > 20:
            raise ValueError
    except ValueError:
        await update.message.reply_text("እባክዎ ከ `0.1` እስከ `20` ኪ.ግ መካከል ትክክለኛ ክብደት ያስገቡ።", parse_mode="Markdown")
        return S_WEIGHT

    billable_kg = calculate_billable_weight(actual_kg)
    is_airport = context.user_data["is_airport_pickup"] or context.user_data["is_airport_dropoff"]
    min_price = calculate_min_price(context.user_data["route"], context.user_data["category"], billable_kg, is_airport)

    context.user_data["actual_weight"] = actual_kg
    context.user_data["billable_weight"] = billable_kg
    context.user_data["min_price"] = min_price

    await update.message.reply_text("የእቃውን ዝርዝር ይጻፉ (ለምሳሌ፡ **ላፕቶፕ** ወይም **2 ኪሎ ልብስ**)፡", parse_mode="Markdown")
    return S_ITEM_DESC


async def s_item_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["item_desc"] = update.message.text.strip()
    d = context.user_data
    suggested_max = round(d["min_price"] * 1.5, 0)

    await update.message.reply_text(
        f"🧮 **የዋጋ ስሌት፡**\n"
        f"• የገባ ክብደት፡ `{d['actual_weight']}` ኪ.ግ ➔ የሚታሰብ፡ **`{d['billable_weight']}` ኪ.ግ**\n"
        f"• ምድብ፡ {d['category']}\n"
        f"• **ዝቅተኛ ዋጋ (Min Offer)፡ `{d['min_price']}` ብር** (የሚመከር ሬንጅ፡ `{d['min_price']}` – `{suggested_max}` ብር)\n\n"
        f"የሚያቀርቡትን የማጓጓዣ ዋጋ በብር ያስገቡ (ቢያንስ `{d['min_price']}` ብር)፡",
        parse_mode="Markdown",
    )
    return S_OFFER_PRICE


async def s_offer_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    min_price = context.user_data["min_price"]
    try:
        offer = float(update.message.text.strip())
        if offer < min_price:
            await update.message.reply_text(f"⚠️ ዋጋው ከዝቅተኛው (`{min_price}` ብር) በታች ነው። ቢያንስ `{min_price}` ብር ያስገቡ፡", parse_mode="Markdown")
            return S_OFFER_PRICE
    except ValueError:
        await update.message.reply_text("እባክዎ ትክክለኛ ቁጥር ያስገቡ።")
        return S_OFFER_PRICE

    context.user_data["delivery_fee"] = offer
    kb = [
        ["ዲሊቨሪ ብቻ (ያለ ኢንሹራንስ - ካሳ እስከ 2x ክፍያ)"],
        ["🛡️ ሙሉ ኢንሹራንስ (3% የእቃ ዋጋ)"],
    ]
    await update.message.reply_text(
        "የኢንሹራንስ አማራጭ ይምረጡ፡",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
    )
    return S_INSURANCE_CHOICE


async def s_insurance_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if "3%" in update.message.text:
        await update.message.reply_text("የእቃውን ግምታዊ ዋጋ በብር ያስገቡ (ለምሳሌ፡ `30000`)፡", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
        return S_ITEM_VALUE
    else:
        context.user_data["insurance_amt"] = 0.0
        return await s_publish_shipment(update, context)


async def s_item_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        val = float(update.message.text.strip())
        if val <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("እባክዎ ትክክለኛ ቁጥር ያስገቡ።")
        return S_ITEM_VALUE

    context.user_data["insurance_amt"] = round(val * INSURANCE_RATE, 2)
    return await s_publish_shipment(update, context)


async def s_publish_shipment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = context.user_data
    commission, payout, total_escrow = recompute_pricing(d["delivery_fee"], d["insurance_amt"])
    pin = str(random.randint(1000, 9999))
    expires_at = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    user = update.effective_user

    conn = sqlite3.connect("adera_logistics.db")
    c = conn.cursor()
    c.execute(
        """INSERT INTO shipments (
            sender_id, sender_type, route, pickup_point, dropoff_point, receiver_info,
            delivery_day, expires_at, category, actual_weight, billable_weight, item_desc,
            min_price, delivery_fee, commission_amt, insurance_amt, traveler_payout,
            total_escrow, delivery_pin, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN_FOR_TRAVELERS')""",
        (
            user.id, d["sender_type"], d["route"], d["pickup_point"], d["dropoff_point"],
            d["receiver_info"], d["delivery_day"], expires_at, d["category"],
            d["actual_weight"], d["billable_weight"], d["item_desc"], d["min_price"],
            d["delivery_fee"], commission, d["insurance_amt"], payout, total_escrow, pin,
        ),
    )
    sid = c.lastrowid
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ **እቃዎ ተለጥፏል! (`ADR-{sid}`)**\n\n"
        f"• መስመር፡ {d['route']} ({d['pickup_point']} ➔ {d['dropoff_point']})\n"
        f"• ተቀባይ፡ {d['receiver_info']}\n"
        f"• ክብደት፡ `{d['billable_weight']}` ኪ.ግ | እቃ፡ {d['item_desc']}\n"
        f"• የቀረበ ዋጋ፡ `{d['delivery_fee']}` ብር (ጠቅላላ ክፍያ ከኢንሹራንስ ጋር፡ `{total_escrow}` ብር)\n"
        f"• 🔐 **የርክክብ ኮድ (PIN)፡ `{pin}`** (እቃው ሲደርስ ለተቀባዩ ብቻ ይስጡ)\n\n"
        "💡 *የP2P ህግ፡ ተጓዥ ሲስማማ ወዲያው ማሳወቂያ ይደርስዎታል፤ ያኔ የኢስክሮ ክፍያ ይከፍላሉ።*",
        reply_markup=get_persistent_menu(),
        parse_mode="Markdown",
    )
    return MAIN_MENU


# ==========================================================
# 6. የተጓዥ ሂደት (TRAVELER FLOW, TERMS, OFFER & COUNTER-OFFER)
# ==========================================================
async def t_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["phone"] = update.message.text.strip()
    await update.message.reply_text("🪪 የ**ፋይዳ (Fayda) መታወቂያዎን** ግልጽ ፎቶ ይላኩ፡", parse_mode="Markdown")
    return T_FAYDA


async def t_fayda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("እባክዎ የፋይዳ መታወቂያ ፎቶ ይላኩ።")
        return T_FAYDA

    context.user_data["fayda_photo_id"] = update.message.photo[-1].file_id
    terms_msg = (
        "📜 **የተጓዥ ህጋዊ ስምምነት (Terms & Full Responsibility)**\n\n"
        "1. እቃውን ሲረከቡ ከፍተው አይተው በፎቶ/ቪዲዮ አረጋግጠው መውሰድ አለብዎት።\n"
        "2. ከተረከቡ በኋላ እስከ መድረሻው ድረስ **ሙሉ ኃላፊነት** የእርስዎ ነው።\n"
        "3. ጥፋት ወይም መጥፋት ቢከሰት በተመዘገበው **የፋይዳ መታወቂያዎ** በህግ ተጠያቂ ይሆናሉ።\n\n"
        "ይስማማሉ?"
    )
    kb = [["✅ እስማማለሁ (Agree)"], ["❌ አልስማማም (Disagree)"]]
    await update.message.reply_text(terms_msg, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True), parse_mode="Markdown")
    return T_TERMS


async def t_terms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if "እስማማለሁ" not in update.message.text:
        await update.message.reply_text("ስምምነቱን ሳይቀበሉ መቀጠል አይቻልም።", reply_markup=get_persistent_menu())
        return MAIN_MENU

    user = update.effective_user
    conn = sqlite3.connect("adera_logistics.db")
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO users (user_id, full_name, role, phone, fayda_photo_id, terms_accepted) VALUES (?, ?, 'TRAVELER', ?, ?, 1)",
        (user.id, user.full_name, context.user_data["phone"], context.user_data["fayda_photo_id"]),
    )
    conn.commit()
    conn.close()

    kb = [["🔍 እቃዎችን መፈለግ (Find Shipments)"], ["✈️ ጉዞ መለጠፍ (Post Trip - እስከ 20 ኪ.ግ)"]]
    await update.message.reply_text("✅ ተመዝግበዋል! ምርጫ ይምረጡ፡", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True))
    return T_ACTION


async def t_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["t_mode"] = "POST_TRIP" if "ጉዞ መለጠፍ" in update.message.text else "FIND"
    routes = [["አዲስ አበባ ➔ አዳማ", "አዲስ አበባ ➔ ሀዋሳ"], ["አዲስ አበባ ➔ ባህር ዳር", "አዲስ አበባ ➔ ድሬዳዋ"]]
    await update.message.reply_text("መስመር ይምረጡ፡", reply_markup=ReplyKeyboardMarkup(routes, resize_keyboard=True, one_time_keyboard=True))
    return T_ROUTE


async def t_route(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["t_route"] = update.message.text.strip()
    days = [["ዛሬ", "ነገ"], ["ሰኞ", "ሌላ ቀን"]]
    await update.message.reply_text("የጉዞ ቀን ይምረጡ ወይም ይጻፉ፡", reply_markup=ReplyKeyboardMarkup(days, resize_keyboard=True, one_time_keyboard=True))
    return T_DAY


async def t_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["t_day"] = update.message.text.strip()
    windows = [["ጠዋት (06:00-12:00)", "ከሰዓት (12:00-18:00)"], ["በረራ ሰዓት", "ማንኛውም ሰዓት"]]
    await update.message.reply_text("የጉዞ / የበረራ ሰዓት ክልል ይምረጡ፡", reply_markup=ReplyKeyboardMarkup(windows, resize_keyboard=True, one_time_keyboard=True))
    return T_WINDOW


async def t_window(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["t_window"] = update.message.text.strip()
    await update.message.reply_text("በሻንጣዎ ስንት ኪ.ግ ቦታ አለዎት? (ከ `1` እስከ `20` ኪ.ግ)፡", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    return T_CAPACITY


async def t_capacity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        cap = float(update.message.text.strip())
        if cap <= 0 or cap > 20:
            raise ValueError
    except ValueError:
        await update.message.reply_text("እባክዎ ከ `1` እስከ `20` ኪ.ግ መካከል ትክክለኛ ቁጥር ያስገቡ።", parse_mode="Markdown")
        return T_CAPACITY

    user = update.effective_user
    d = context.user_data

    if d["t_mode"] == "POST_TRIP":
        conn = sqlite3.connect("adera_logistics.db")
        c = conn.cursor()
        c.execute(
            "INSERT INTO trips (traveler_id, route, travel_day, time_window, total_capacity_kg, remaining_capacity_kg) VALUES (?, ?, ?, ?, ?, ?)",
            (user.id, d["t_route"], d["t_day"], d["t_window"], cap, cap),
        )
        tid = c.lastrowid
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ ጉዞዎ ተለጥፏል (`TRIP-{tid}`) — {d['t_route']} | {d['t_day']} ({d['t_window']}) | ቦታ፡ `{cap}` ኪ.ግ",
            reply_markup=get_persistent_menu(),
            parse_mode="Markdown",
        )
        return MAIN_MENU

    conn = sqlite3.connect("adera_logistics.db")
    c = conn.cursor()
    c.execute(
        """SELECT shipment_id, pickup_point, dropoff_point, delivery_day, item_desc, billable_weight, traveler_payout, delivery_fee
           FROM shipments WHERE route = ? AND status = 'OPEN_FOR_TRAVELERS' AND billable_weight <= ?""",
        (d["t_route"], cap),
    )
    rows = c.fetchall()
    conn.close()

    await update.message.reply_text(f"🔍 በ **{d['t_route']}** ያሉ እቃዎች (ቦታዎ፡ `{cap}` ኪ.ግ)：", reply_markup=get_persistent_menu(), parse_mode="Markdown")
    if not rows:
        await update.message.reply_text("በአሁኑ ሰዓት የሚስማማ እቃ የለም።")
        return MAIN_MENU

    for r in rows:
        sid, pickup, dropoff, d_day, desc, b_kg, payout, fee = r
        txt = (
            f"🆔 `ADR-{sid}` | 📦 {desc} (`{b_kg}` ኪ.ግ)\n"
            f"📍 {pickup} ➔ {dropoff} | 🕒 {d_day}\n"
            f"💰 የተጣራ ክፍያዎ (88%)፡ `{payout}` ብር (ጠቅላላ ዋጋ፡ `{fee}` ብር)"
        )
        btns = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ በዚህ ዋጋ ልውሰድ", callback_data=f"t_accept_{sid}"),
            InlineKeyboardButton("💬 ሌላ ዋጋ ላቅርብ", callback_data=f"t_counter_{sid}"),
        ]])
        await update.message.reply_text(txt, reply_markup=btns, parse_mode="Markdown")

    return MAIN_MENU


async def show_active_trips(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    conn = sqlite3.connect("adera_logistics.db")
    c = conn.cursor()
    c.execute("SELECT trip_id, route, travel_day, time_window, remaining_capacity_kg FROM trips WHERE status = 'ACTIVE' AND remaining_capacity_kg > 0")
    trips = c.fetchall()
    conn.close()

    if not trips:
        await update.message.reply_text("በአሁኑ ሰዓት የተለጠፈ ክፍት ጉዞ የለም።", reply_markup=get_persistent_menu())
        return MAIN_MENU

    msg = "✈️ **የተለጠፉ የተጓዦች ጉዞዎች：**\n\n"
    for t in trips:
        msg += f"• `TRIP-{t[0]}` | {t[1]} | ቀን፡ {t[2]} ({t[3]}) | ቀሪ ቦታ፡ **`{t[4]}` ኪ.ግ**\n"
    await update.message.reply_text(msg, reply_markup=get_persistent_menu(), parse_mode="Markdown")
    return MAIN_MENU


# ==========================================================
# 7. CALLBACKS: ACCEPT, COUNTER-OFFER, ESCROW & ADMIN
# ==========================================================
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    conn = sqlite3.connect("adera_logistics.db")
    c = conn.cursor()

    if data.startswith("t_accept_"):
        sid = int(data.split("_")[2])
        c.execute("SELECT sender_id, item_desc, delivery_fee, insurance_amt FROM shipments WHERE shipment_id = ? AND status = 'OPEN_FOR_TRAVELERS'", (sid,))
        row = c.fetchone()
        if not row:
            await query.edit_message_text("⚠️ ይህ ትዕዛዝ ቀድሞ ተይዟል ወይም ተዘግቷል።")
            conn.close()
            return MAIN_MENU

        sender_id, desc, fee, ins = row
        comm, payout, total_escrow = recompute_pricing(fee, ins)
        c.execute(
            "UPDATE shipments SET traveler_id = ?, commission_amt = ?, traveler_payout = ?, total_escrow = ?, status = 'AWAITING_ESCROW_PAYMENT' WHERE shipment_id = ?",
            (user.id, comm, payout, total_escrow, sid),
        )
        conn.commit()
        conn.close()

        await query.edit_message_text(f"✅ ጥያቄዎ ለ `ADR-{sid}` ({desc}) ተልኳል። ላኪው ኢስክሮ ክፍያውን ሲፈጽም ወዲያው ማሳወቂያ ይደርስዎታል።")
        try:
            await context.bot.send_message(
                chat_id=sender_id,
                text=(
                    f"🎉 **ተጓዥ ተስማምቷል (`ADR-{sid}`)!**\n"
                    f"ተጓዥ፡ **{user.full_name}** (`{user.id}`) እቃዎን ({desc}) ለማጓጓዝ ተቀብሏል።\n\n"
                    f"💳 **ጠቅላላ የኢስክሮ ክፍያ፡ `{total_escrow}` ብር**\n"
                    f"{PAYMENT_ACCOUNTS}\n\n"
                    f"ክፍያ ከፈጸሙ በኋላ `/start` -> `📦 እቃ መላክ` ሳይሆን በቀጥታ የደረሰኝ ፎቶ/ID ይላኩ (ወይም `ADR-{sid}: [Receipt ID]` ብለው በ Chat ይላኩ)።"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass

    elif data.startswith("t_counter_"):
        sid = int(data.split("_")[2])
        context.user_data["counter_sid"] = sid
        conn.close()
        await query.message.reply_text(f"💬 ለትዕዛዝ `ADR-{sid}` የሚያቀርቡትን ዋጋ በቁጥር (ብር) ያስገቡ፡", parse_mode="Markdown")
        return COUNTER_OFFER_INPUT

    elif data.startswith("adm_approve_"):
        sid = int(data.split("_")[2])
        c.execute("SELECT sender_id, traveler_id, delivery_pin, receiver_info FROM shipments WHERE shipment_id = ?", (sid,))
        row = c.fetchone()
        if row:
            sender_id, traveler_id, pin, recv = row
            c.execute("UPDATE shipments SET status = 'ESCROW_HELD' WHERE shipment_id = ?", (sid,))
            conn.commit()
            await query.edit_message_caption(caption=f"✅ `ADR-{sid}` ክፍያ ተረጋግጧል (`ESCROW_HELD`)።") if query.message.caption else await query.edit_message_text(f"✅ `ADR-{sid}` ክፍያ ተረጋግጧል (`ESCROW_HELD`)።")
            try:
                await context.bot.send_message(
                    chat_id=sender_id,
                    text=f"✅ **ክፍያዎ በአደራ (Escrow) ተይዟል (`ADR-{sid}`)!**\nየርክክብ ኮድ (PIN)፡ `{pin}`\nእቃውን ለተጓዡ ሲያስረክቡ ተጓዡ በፎቶ/ቪዲዮ እንዲያረጋግጥ ይጠይቁ።",
                    parse_mode="Markdown",
                )
                if traveler_id:
                    await context.bot.send_message(
                        chat_id=traveler_id,
                        text=f"💳 የትዕዛዝ `ADR-{sid}` ኢስክሮ ክፍያ ተረጋግጧል! እቃውን ተረክበው በ `📸/🎥 ርክክብ ፎቶ/ቪዲዮ` ያረጋግጡ። ተቀባይ፡ {recv}",
                        parse_mode="Markdown",
                    )
            except Exception:
                pass
        conn.close()

    elif data.startswith("adm_reject_"):
        sid = int(data.split("_")[2])
        c.execute("UPDATE shipments SET status = 'PAYMENT_REJECTED' WHERE shipment_id = ?", (sid,))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"❌ `ADR-{sid}` ክፍያ ውድቅ ተደርጓል።")

    elif data.startswith("rate_"):
        # format: rate_{sid}_{stars}
        parts = data.split("_")
        sid, stars = int(parts[1]), int(parts[2])
        c.execute("SELECT traveler_id FROM shipments WHERE shipment_id = ?", (sid,))
        row = c.fetchone()
        if row and row[0]:
            tid = row[0]
            c.execute("UPDATE users SET rating_sum = rating_sum + ?, rating_count = rating_count + 1 WHERE user_id = ?", (stars, tid))
            conn.commit()
        conn.close()
        await query.edit_message_text(f"⭐ ለትዕዛዝ `ADR-{sid}` **{stars} ኮከብ** ደረጃ ሰጥተዋል። እናመሰግናለን!", parse_mode="Markdown")

    conn.close()
    return MAIN_MENU


async def counter_offer_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        new_fee = float(update.message.text.strip())
        if new_fee <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("እባክዎ ትክክለኛ ዋጋ በቁጥር ያስገቡ።")
        return COUNTER_OFFER_INPUT

    sid = context.user_data.get("counter_sid")
    traveler = update.effective_user
    conn = sqlite3.connect("adera_logistics.db")
    c = conn.cursor()
    c.execute("SELECT sender_id, item_desc FROM shipments WHERE shipment_id = ?", (sid,))
    row = c.fetchone()
    conn.close()

    if row:
        sender_id, desc = row
        await update.message.reply_text(f"✅ የ `{new_fee}` ብር ዋጋ ሀሳብ ለላኪው ተልኳል።", reply_markup=get_persistent_menu(), parse_mode="Markdown")
        try:
            await context.bot.send_message(
                chat_id=sender_id,
                text=(
                    f"💬 **የዋጋ ድርድር (Counter-Offer) ለ `ADR-{sid}` ({desc})**\n"
                    f"ተጓዥ **{traveler.full_name}** (`{traveler.id}`) በ **`{new_fee}` ብር** ለማድረስ ሀሳብ አቅርቧል።\n"
                    f"ለመስማማት በ `💬 ለአጋር መልዕክት (Chat)` በኩል `ADR-{sid}: በ {new_fee} ብር ተስማምቻለሁ` ብለው ይላኩ።"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass
    return MAIN_MENU


# ==========================================================
# 8. PICKUP PHOTO/VIDEO, RELAY CHAT, PIN, CANCEL & DISPUTE
# ==========================================================
async def pickup_media_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    caption = (update.message.caption or "").upper().replace("ADR-", "").strip()
    try:
        sid = int(caption)
    except ValueError:
        await update.message.reply_text("⚠️ እባክዎ ፎቶውን ወይም ቪዲዮውን ሲልኩ በ **Caption** ላይ የትዕዛዝ ቁጥሩን (ለምሳሌ `1` ወይም `ADR-1`) ይጻፉ፡", reply_markup=get_persistent_menu(), parse_mode="Markdown")
        return MAIN_MENU

    media_type, media_id = None, None
    if update.message.photo:
        media_type, media_id = "PHOTO", update.message.photo[-1].file_id
    elif update.message.video:
        media_type, media_id = "VIDEO", update.message.video.file_id
    else:
        await update.message.reply_text("እባክዎ የእቃውን ፎቶ ወይም አጭር ቪዲዮ ይላኩ።", reply_markup=get_persistent_menu())
        return MAIN_MENU

    conn = sqlite3.connect("adera_logistics.db")
    c = conn.cursor()
    c.execute("UPDATE shipments SET pickup_media_type = ?, pickup_media_id = ?, status = 'IN_TRANSIT' WHERE shipment_id = ?", (media_type, media_id, sid))
    c.execute("SELECT sender_id, receiver_info FROM shipments WHERE shipment_id = ?", (sid,))
    row = c.fetchone()
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ የመረከቢያ {media_type} ለ `ADR-{sid}` ተመዝግቧል! ሁኔታው ወደ **`IN_TRANSIT`** ተቀይሯል።\n"
        f"መድረሻ ሲደርሱ ተቀባዩን (`{row[1] if row else ''}`) አግኝተው 4-አሃዝ PIN ያስገቡ።",
        reply_markup=get_persistent_menu(),
        parse_mode="Markdown",
    )
    if row:
        try:
            if media_type == "PHOTO":
                await context.bot.send_photo(chat_id=row[0], photo=media_id, caption=f"📸 ተጓዡ እቃውን (`ADR-{sid}`) አይቶ ተረክቧል (`IN_TRANSIT`)።")
            else:
                await context.bot.send_video(chat_id=row[0], video=media_id, caption=f"🎥 ተጓዡ እቃውን (`ADR-{sid}`) አይቶ ተረክቧል (`IN_TRANSIT`)።")
        except Exception:
            pass
    return MAIN_MENU


async def relay_chat_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if ":" not in text:
        await update.message.reply_text("⚠️ እባክዎ በዚህ መልኩ ይጻፉ፡ `ADR-1: መልዕክትዎ እዚህ`", reply_markup=get_persistent_menu(), parse_mode="Markdown")
        return MAIN_MENU

    prefix, body = text.split(":", 1)
    sid_str = prefix.upper().replace("ADR-", "").strip()
    try:
        sid = int(sid_str)
    except ValueError:
        await update.message.reply_text("የትዕዛዝ ቁጥሩ ትክክል አይደለም።", reply_markup=get_persistent_menu())
        return MAIN_MENU

    sender_user = update.effective_user
    conn = sqlite3.connect("adera_logistics.db")
    c = conn.cursor()
    c.execute("SELECT sender_id, traveler_id FROM shipments WHERE shipment_id = ?", (sid,))
    row = c.fetchone()
    conn.close()

    if not row:
        await update.message.reply_text("ትዕዛዙ አልተገኘም።", reply_markup=get_persistent_menu())
        return MAIN_MENU

    s_id, t_id = row
    target_id = t_id if sender_user.id == s_id else s_id
    if not target_id:
        await update.message.reply_text("ለዚህ ትዕዛዝ የተመደበ አጋር ገና የለም።", reply_markup=get_persistent_menu())
        return MAIN_MENU

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"💬 **መልዕክት ለ `ADR-{sid}` ከ {sender_user.first_name}：**\n{body.strip()}",
            parse_mode="Markdown",
        )
        await update.message.reply_text("✅ መልዕክትዎ በአስተማማኝ ተልኳል።", reply_markup=get_persistent_menu())
    except Exception:
        await update.message.reply_text("መልዕክቱን መላክ አልተቻለም።", reply_markup=get_persistent_menu())

    return MAIN_MENU


async def track_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().upper().replace("ADR-", "").replace("#", "")
    try:
        sid = int(raw)
    except ValueError:
        await update.message.reply_text("እባክዎ ትክክለኛ የትዕዛዝ ቁጥር ያስገቡ (ለምሳሌ `ADR-1`)።", reply_markup=get_persistent_menu(), parse_mode="Markdown")
        return MAIN_MENU

    conn = sqlite3.connect("adera_logistics.db")
    c = conn.cursor()
    c.execute(
        "SELECT shipment_id, route, pickup_point, dropoff_point, receiver_info, delivery_day, expires_at, item_desc, status FROM shipments WHERE shipment_id = ?",
        (sid,),
    )
    row = c.fetchone()
    conn.close()

    if row:
        await update.message.reply_text(
            f"📍 **የእቃ መከታተያ (`ADR-{row[0]}`)**\n"
            f"• መስመር፡ {row[1]} ({row[2]} ➔ {row[3]})\n"
            f"• ተቀባይ፡ {row[4]}\n"
            f"• የሚፈለግበት ቀን፡ {row[5]} (Expiration: {row[6]})\n"
            f"• እቃ፡ {row[7]}\n"
            f"• ሁኔታ፡ **`{row[8]}`**",
            reply_markup=get_persistent_menu(),
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("❌ ትዕዛዙ አልተገኘም።", reply_markup=get_persistent_menu())
    return MAIN_MENU


async def verify_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pin = update.message.text.strip()
    conn = sqlite3.connect("adera_logistics.db")
    c = conn.cursor()
    c.execute("SELECT shipment_id, sender_id, traveler_payout, commission_amt FROM shipments WHERE delivery_pin = ? AND status != 'COMPLETED'", (pin,))
    row = c.fetchone()

    if row:
        sid, sender_id, payout, comm = row
        c.execute("UPDATE shipments SET status = 'COMPLETED' WHERE shipment_id = ?", (sid,))
        conn.commit()
        await update.message.reply_text(
            f"🎉 **ርክክብ ተጠናቋል (`ADR-{sid}`)!**\n"
            f"• የተጓዥ ክፍያ፡ `{payout}` ብር\n"
            f"• የአደራ ኮሚሽን፡ `{comm}` ብር",
            reply_markup=get_persistent_menu(),
            parse_mode="Markdown",
        )
        # ለላኪው የ 1-5 ኮከብ Rating ጥያቄ መላክ
        rating_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⭐ 1", callback_data=f"rate_{sid}_1"),
            InlineKeyboardButton("⭐⭐ 2", callback_data=f"rate_{sid}_2"),
            InlineKeyboardButton("⭐⭐⭐ 3", callback_data=f"rate_{sid}_3"),
            InlineKeyboardButton("⭐⭐⭐⭐ 4", callback_data=f"rate_{sid}_4"),
            InlineKeyboardButton("⭐⭐⭐⭐⭐ 5", callback_data=f"rate_{sid}_5"),
        ]])
        try:
            await context.bot.send_message(
                chat_id=sender_id,
                text=f"🌟 ትዕዛዝ `ADR-{sid}` በተሳካ ሁኔታ ደርሷል! ለተጓዡ ደረጃ (Rating) ይስጡ፡",
                reply_markup=rating_kb,
                parse_mode="Markdown",
            )
        except Exception:
            pass
    else:
        await update.message.reply_text("❌ የተሳሳተ PIN ወይም ቀድሞ የተጠናቀቀ ትዕዛዝ ነው።", reply_markup=get_persistent_menu())

    conn.close()
    return MAIN_MENU


async def dispute_order_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().upper().replace("ADR-", "").replace("#", "")
    try:
        sid = int(raw)
    except ValueError:
        await update.message.reply_text("እባክዎ ትክክለኛ የትዕዛዝ ቁጥር ያስገቡ።", reply_markup=get_persistent_menu())
        return MAIN_MENU

    context.user_data["dispute_sid"] = sid
    await update.message.reply_text(f"ለትዕዛዝ `ADR-{sid}` የስረዛ / የገንዘብ ተመላሽ / የችግር ምክንያቱን በአጭሩ ይጻፉ፡", parse_mode="Markdown")
    return DISPUTE_REASON


async def dispute_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reason = update.message.text.strip()
    sid = context.user_data.get("dispute_sid")
    user = update.effective_user
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = sqlite3.connect("adera_logistics.db")
    c = conn.cursor()
    c.execute("INSERT INTO disputes (shipment_id, reporter_id, reason, created_at) VALUES (?, ?, ?, ?)", (sid, user.id, reason, now_str))
    c.execute("UPDATE shipments SET status = 'CANCELLED_OR_DISPUTED' WHERE shipment_id = ?", (sid,))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ ጥያቄዎ ለ `ADR-{sid}` ተመዝግቧል። የአደራ ቡድን ጉዳዩን አይቶ የገንዘብ ተመላሽ (Refund) ወይም እልባት ይሰጣል።",
        reply_markup=get_persistent_menu(),
        parse_mode="Markdown",
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"⚠️ **ስረዛ / ችግር ሪፖርት (`ADR-{sid}`)**\n• ሪፖርት አድራጊ፡ {user.full_name} (`{user.id}`)\n• ምክንያት፡ {reason}",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    return MAIN_MENU


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router)],
        states={
            MAIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router)],
            S_ACCOUNT_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_account_type)],
            S_ROUTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_route)],
            S_PICKUP_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_pickup_type)],
            S_PICKUP_DETAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_pickup_detail)],
            S_DROPOFF_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_dropoff_type)],
            S_DROPOFF_DETAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_dropoff_detail)],
            S_RECEIVER_INFO: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_receiver_info)],
            S_DELIVERY_DAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_delivery_day)],
            S_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_category)],
            S_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_weight)],
            S_ITEM_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_item_desc)],
            S_OFFER_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_offer_price)],
            S_INSURANCE_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_insurance_choice)],
            S_ITEM_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, s_item_value)],
            S_PAYMENT_PROOF: [MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, s_payment_proof)],
            T_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_phone)],
            T_FAYDA: [MessageHandler(filters.PHOTO, t_fayda)],
            T_TERMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_terms)],
            T_ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_action)],
            T_ROUTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_route)],
            T_DAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_day)],
            T_WINDOW: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_window)],
            T_CAPACITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_capacity)],
            PICKUP_MEDIA_PROOF: [MessageHandler((filters.PHOTO | filters.VIDEO) & ~filters.COMMAND, pickup_media_proof)],
            COUNTER_OFFER_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, counter_offer_input)],
            RELAY_CHAT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, relay_chat_input)],
            VERIFY_PIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, verify_pin)],
            TRACK_ORDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, track_order)],
            DISPUTE_ORDER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, dispute_order_id)],
            DISPUTE_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, dispute_reason)],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(callback_router))
    print("ADERA Ethiopia P2P Bot (Full Edition) 24/7 ዝግጁ ነው...")
    app.run_polling()


if __name__ == "__main__":
    main()