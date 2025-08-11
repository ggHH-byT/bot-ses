import asyncio, os, re, json, logging, signal, random
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# === Загружаем настройки ===
load_dotenv()

API_ID = int(os.getenv("TG_API_ID"))
API_HASH = os.getenv("TG_API_HASH")
SESSION = os.getenv("SESSION_NAME", "user_gifts_session")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
MAX_BUYS_PER_CYCLE = int(os.getenv("MAX_BUYS_PER_CYCLE", "5"))
PREMIUM_WORDS = [w.strip().lower() for w in os.getenv("PREMIUM_WORDS", "premium,премиум").split(",")]
NEW_NOTIFY_LIMIT = int(os.getenv("NEW_NOTIFY_LIMIT", "20"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

STORAGE = "tg_storage_state.json"
BOUGHT_FILE = Path("bought_titles.json")
SEEN_FILE = Path("seen_gifts.json")

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s: %(message)s")
LOG = logging.getLogger("gifts")

# === Селекторы ===
ENTRY_SEND_GIFT = "button:has-text('Отправить подарок'), button:has-text('Send a gift')"
ENTRY_SEND_TO_SELF = "button:has-text('Отправить себе'), button:has-text('Send to myself')"
ALL_TAB = "button:has-text('Все подарки'), button:has-text('All gifts')"
CARD_ITEM = "[data-test-id='gift-card'], .gift-card, [class*='giftCard'], [class*='GiftCard']"
CARD_TITLE = ".title, [data-test-id='gift-title'], [class*='Title']"
CARD_BADGE = ".badge, .label, [data-test-id='gift-badge'], [class*='Badge']"
CARD_FRAME = ".card, .frame, .container, [class*='card']"
BUY_BTN_LIST = ("button:has-text('Купить'), button:has-text('Buy'), "
                "button:has-text('Отправить'), button:has-text('Send')")
CONFIRM_BUY_BTN = ("button:has-text('ОТПРАВИТЬ ПОДАРОК'), "
                   "button:has-text('Отправить подарок'), button:has-text('Send gift')")
GIFT_ENTRY_URL = "https://t.me/gifts"

# === Хранилища состояний ===
def load_set(file: Path) -> set:
    if file.exists():
        try:
            return set(json.loads(file.read_text("utf-8")))
        except:
            return set()
    return set()

def save_set(file: Path, s: set):
    file.write_text(json.dumps(sorted(s), ensure_ascii=False), encoding="utf-8")

# === Логика премиум ===
def looks_premium(title:str, badge:str) -> bool:
    t = (title or "").lower()
    b = (badge or "").lower()
    return any(w in t for w in PREMIUM_WORDS) or any(w in b for w in PREMIUM_WORDS)

async def has_colored_border(card) -> bool:
    try:
        elem = card.locator(CARD_FRAME).first
        if await elem.count() == 0:
            elem = card
        color = await elem.evaluate("""(el)=>{
            const s = getComputedStyle(el);
            return (s.borderColor || s.outlineColor || s.boxShadow || '').toString();
        }""")
        if not color:
            return False
        color = color.lower()
        if "transparent" in color or "none" == color.strip():
            return False
        m = re.search(r"rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)", color)
        if m:
            r,g,b = map(int, m.groups())
            return not (abs(r-g)<8 and abs(g-b)<8 and abs(r-b)<8)
        return True
    except:
        return False

# === Playwright шаги ===
async def ensure_login(context):
    page = await context.new_page()
    try:
        await page.goto("https://web.telegram.org/k/", wait_until="networkidle", timeout=60000)
        if "login" in page.url or "auth" in page.url:
            LOG.info("Выполни вход в Telegram Web (QR/код). Жду…")
            await page.wait_for_url(re.compile(r".*/k/.*"), timeout=0)
            await context.storage_state(path=STORAGE)
            LOG.info("Сессия сохранена.")
    finally:
        await page.close()

async def open_gifts_webapp(page):
    await page.goto(GIFT_ENTRY_URL, wait_until="domcontentloaded", timeout=60000)
    for text in ("Open", "Открыть"):
        try:
            await page.get_by_text(text).click(timeout=3000)
            break
        except:
            pass
    await page.wait_for_timeout(1200)
    frames = [f for f in page.frames if f != page.main_frame]
    return frames[-1] if frames else page.main_frame

async def enter_catalog(webview):
    try:
        if await webview.locator(ENTRY_SEND_GIFT).count():
            await webview.locator(ENTRY_SEND_GIFT).first.click()
            await webview.wait_for_timeout(350)
        if await webview.locator(ENTRY_SEND_TO_SELF).count():
            await webview.locator(ENTRY_SEND_TO_SELF).first.click()
            await webview.wait_for_timeout(500)
    except:
        pass

async def refresh_app(webview):
    try:
        if await webview.locator(ALL_TAB).count():
            await webview.locator(ALL_TAB).first.click()
            await webview.wait_for_timeout(350)
    except:
        pass

async def scan_and_buy(webview, bought_titles, seen_gifts, max_buys, client):
    buys = 0
    new_notified = 0
    cards = await webview.locator(CARD_ITEM).all()
    LOG.info("Карточек на экране: %d", len(cards))

    for card in cards:
        title = badge = ""
        try:
            if await card.locator(CARD_TITLE).count():
                title = (await card.locator(CARD_TITLE).first.inner_text()).strip()
            if await card.locator(CARD_BADGE).count():
                badge = (await card.locator(CARD_BADGE).first.inner_text()).strip()
        except:
            pass

        key = title or f"html:{await card.evaluate('(e)=>e.outerHTML')[:64]}"

        if key not in seen_gifts and new_notified < NEW_NOTIFY_LIMIT:
            try:
                await client.send_message("me", f"🆕 Новый подарок: {title or '(без названия)'}")
            except:
                pass
            seen_gifts.add(key)
            save_set(SEEN_FILE, seen_gifts)
            new_notified += 1

        is_premium = looks_premium(title, badge) or await has_colored_border(card)
        if not is_premium or key in bought_titles:
            continue

        try:
            await card.click()
            await webview.locator(BUY_BTN_LIST).first.click(timeout=5000)
            btn = webview.locator(CONFIRM_BUY_BTN).first
            await btn.wait_for(timeout=6000)
            price_txt = await btn.inner_text()
            await btn.click()
            LOG.info("✅ Куплено: %s", title)
            bought_titles.add(key)
            save_set(BOUGHT_FILE, bought_titles)
            await client.send_message("me", f"✅ Куплен подарок: {title} ({price_txt})")
            buys += 1
            if buys >= max_buys:
                break
            await asyncio.sleep(1.0 + random.random()*0.7)
        except:
            continue
    return buys

# === Главный цикл ===
async def main():
    bought_titles = load_set(BOUGHT_FILE)
    seen_gifts = load_set(SEEN_FILE)

    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()

    while True:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                context = await browser.new_context(
                    storage_state=STORAGE if Path(STORAGE).exists() else None
                )
                await ensure_login(context)
                page = await context.new_page()
                try:
                    webview = await open_gifts_webapp(page)
                    await enter_catalog(webview)
                    await refresh_app(webview)
                    await scan_and_buy(webview, bought_titles, seen_gifts, MAX_BUYS_PER_CYCLE, client)
                finally:
                    await page.close()
                    await browser.close()
        except PlaywrightTimeoutError:
            LOG.warning("Не удалось загрузить Telegram Web, повтор через %s сек.", CHECK_INTERVAL)
        except Exception as e:
            LOG.error("Ошибка в цикле: %s", e)

        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_DFL)
    asyncio.run(main())

