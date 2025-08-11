import asyncio, os, re, json, logging, signal, random
from pathlib import Path

from dotenv import load_dotenv
from pyrogram import Client
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------- Конфиг и состояния ----------------------
load_dotenv()

API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
SESSION_NAME = os.getenv("SESSION_NAME", "pyro_user")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
MAX_BUYS_PER_CYCLE = int(os.getenv("MAX_BUYS_PER_CYCLE", "5"))
NEW_NOTIFY_LIMIT = int(os.getenv("NEW_NOTIFY_LIMIT", "20"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
PREMIUM_WORDS = [w.strip().lower() for w in os.getenv("PREMIUM_WORDS", "premium,премиум").split(",")]
PROXY_SERVER = os.getenv("PROXY_SERVER")  # пример: http://user:pass@host:port

STORAGE = "tg_storage_state.json"      # сессия Telegram Web (Playwright)
BOUGHT_FILE = Path("bought_titles.json")  # что уже купили
SEEN_FILE = Path("seen_gifts.json")       # что уже видели (для уведомлений о новых)

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s: %(message)s")
LOG = logging.getLogger("gifts")

# Telegram Web варианты (иногда ломается одна ветка)
TG_WEB_URLS = [
    "https://web.telegram.org/k/",
    "https://web.telegram.org/a/",
    "https://web.telegram.org/z/",
]
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/123.0.0.0 Safari/537.36")
GIFT_ENTRY_URL = "https://t.me/gifts"

# ---------------------- Селекторы в мини‑аппе ----------------------
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

# ---------------------- Утиль для set файлов ----------------------
def load_set(path: Path) -> set:
    if path.exists():
        try:
            return set(json.loads(path.read_text("utf-8")))
        except Exception:
            return set()
    return set()

def save_set(path: Path, s: set):
    path.write_text(json.dumps(sorted(s), ensure_ascii=False), encoding="utf-8")

# ---------------------- Логика детекции премиума ----------------------
def looks_premium(title: str, badge: str) -> bool:
    t = (title or "").lower()
    b = (badge or "").lower()
    return any(w in t for w in PREMIUM_WORDS) or any(w in b for w in PREMIUM_WORDS)

async def has_colored_border(card) -> bool:
    """Эвристика «цветной обводки»: рамка/тень не серого тона."""
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
        if "transparent" in color or color.strip() == "none":
            return False
        m = re.search(r"rgb\(\s*(\d+),\s*(\d+),\s*(\d+)\s*\)", color)
        if m:
            r, g, b = map(int, m.groups())
            # не почти серый (r≈g≈b)
            return not (abs(r-g) < 8 and abs(g-b) < 8 and abs(r-b) < 8)
        # если нет rgb — считаем, что оформление «цветное»
        return True
    except:
        return False

# ---------------------- Playwright-шаги ----------------------
async def ensure_login(context):
    page = await context.new_page()
    try:
        loaded = False
        # 1: пробуем полную загрузку
        for url in TG_WEB_URLS:
            try:
                await page.goto(url, wait_until="networkidle", timeout=60000)
                loaded = True
                break
            except Exception:
                continue
        # 2: fallback — подлиннее таймаут и domcontentloaded
        if not loaded:
            for url in TG_WEB_URLS:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                    try:
                        await page.wait_for_selector("div,button,input", timeout=15000)
                    except Exception:
                        pass
                    loaded = True
                    break
                except Exception:
                    continue
        if not loaded:
            raise RuntimeError("Telegram Web недоступен по всем URL")

        if "login" in page.url or "auth" in page.url:
            LOG.info("Выполни вход в Telegram Web (QR/код). Жду…")
            await page.wait_for_url(re.compile(r".*/(k|a|z)/.*"), timeout=0)
            await context.storage_state(path=STORAGE)
            LOG.info("Сессия сохранена.")
    finally:
        await page.close()

async def open_gifts_webapp(page):
    await page.goto(GIFT_ENTRY_URL, wait_until="domcontentloaded", timeout=90000)
    for text in ("Open", "Открыть"):
        try:
            await page.get_by_text(text).click(timeout=5000)
            break
        except:
            pass
    await page.wait_for_timeout(1500)
    frames = [f for f in page.frames if f != page.main_frame]
    return frames[-1] if frames else page.main_frame

async def enter_catalog(webview):
    try:
        if await webview.locator(ENTRY_SEND_GIFT).count():
            await webview.locator(ENTRY_SEND_GIFT).first.click()
            await webview.wait_for_timeout(350)
    except:
        pass
    try:
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

async def scan_and_buy(webview, bought_titles: set, seen_gifts: set,
                       max_buys: int, pyro: Client) -> int:
    """Сканируем каталог: уведомляем о новых, покупаем премиум."""
    buys = 0
    new_notified = 0

    cards = await webview.locator(CARD_ITEM).all()
cards_count = len(cards)

if cards_count == 0:
    LOG.warning("⚠️ Карточек на экране = 0 — сохраняю скриншот и HTML для отладки...")
    await webview.screenshot(path="debug_no_cards.png", full_page=True)
    html_content = await webview.content()
    with open("debug_no_cards.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    LOG.info("💾 Файлы сохранены: debug_no_cards.png и debug_no_cards.html")

    # Повторная попытка через 3 сек
    await asyncio.sleep(3)
    cards = await webview.locator(CARD_ITEM).all()
    cards_count = len(cards)

LOG.info("Карточек на экране: %d", cards_count)


    for card in cards:
        title = badge = ""
        try:
            if await card.locator(CARD_TITLE).count():
                title = (await card.locator(CARD_TITLE).first.inner_text()).strip()
            if await card.locator(CARD_BADGE).count():
                badge = (await card.locator(CARD_BADGE).first.inner_text()).strip()
        except:
            pass

        # устойчивый ключ
        try:
            html_snip = await card.evaluate("(e)=>e.outerHTML")
        except:
            html_snip = ""
        key = title or f"html:{html_snip.strip()[:96]}"

        # уведомления о НОВЫХ карточках
        if key not in seen_gifts and new_notified < NEW_NOTIFY_LIMIT:
            try:
                await pyro.send_message("me", f"🆕 Новый подарок: {title or '(без названия)'}")
            except Exception as e:
                LOG.warning("Не отправилось уведомление о новом подарке: %s", e)
            seen_gifts.add(key)
            save_set(SEEN_FILE, seen_gifts)
            new_notified += 1

        # детекция премиума
        is_premium = looks_premium(title, badge)
        if not is_premium:
            is_premium = await has_colored_border(card)
        if not is_premium:
            continue

        if key in bought_titles:
            continue

        LOG.info("🔎 Премиум: %r / %r", title, badge)

        # покупка
        try:
            await card.click()
            await webview.locator(BUY_BTN_LIST).first.click(timeout=5000)

            btn = webview.locator(CONFIRM_BUY_BTN).first
            await btn.wait_for(timeout=6000)
            price_txt = (await btn.inner_text()).strip()
            await btn.click()

            LOG.info("✅ Куплено: %s (%s)", title or key, price_txt)
            bought_titles.add(key)
            save_set(BOUGHT_FILE, bought_titles)

            try:
                await pyro.send_message("me", f"✅ Куплен подарок: {title or key} {('('+price_txt+')') if price_txt else ''}")
            except Exception as e:
                LOG.warning("Не отправилось в 'Избранное': %s", e)

            buys += 1
            if buys >= max_buys:
                break
            await asyncio.sleep(1.0 + random.random()*0.7)

        except Exception as e:
            LOG.warning("Покупка не удалась: %s", e)
            # пробуем вернуться назад и сканить дальше
            try:
                await webview.go_back()
            except:
                pass

    return buys

# ---------------------- Главный цикл ----------------------
async def run():
    # Pyrogram — один раз на всю жизнь процесса
    pyro = Client(
        name=SESSION_NAME,
        api_id=API_ID,
        api_hash=API_HASH,
        workdir=".",  # сессия будет pyro_user.session в текущем каталоге
        no_updates=True
    )
    await pyro.start()  # спросит код/2FA при первом запуске

    bought_titles = load_set(BOUGHT_FILE)
    seen_gifts = load_set(SEEN_FILE)

    while True:
        try:
            async with async_playwright() as p:
                launch_kwargs = {
                    "headless": True,
                    "args": [
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-blink-features=AutomationControlled",
                    ],
                }
                if PROXY_SERVER:
                    launch_kwargs["proxy"] = {"server": PROXY_SERVER}

                browser = await p.chromium.launch(**launch_kwargs)

                context_kwargs = {
                    "storage_state": STORAGE if Path(STORAGE).exists() else None,
                    "user_agent": USER_AGENT,
                    "viewport": {"width": 1366, "height": 768},
                }
                # proxy на уровне контекста тоже можно задать при необходимости:
                # if PROXY_SERVER:
                #     context_kwargs["proxy"] = {"server": PROXY_SERVER}

                context = await browser.new_context(**context_kwargs)

                try:
                    await ensure_login(context)
                    page = await context.new_page()

                    try:
                        webview = await open_gifts_webapp(page)
                        await enter_catalog(webview)
                        await refresh_app(webview)
                        await scan_and_buy(webview, bought_titles, seen_gifts, MAX_BUYS_PER_CYCLE, pyro)
                    finally:
                        await page.close()

                finally:
                    await context.close()
                    await browser.close()

        except PlaywrightTimeoutError:
            LOG.warning("Не удалось загрузить Telegram Web, повтор через %s сек.", CHECK_INTERVAL)
        except Exception as e:
            LOG.error("Ошибка в цикле: %s", e)

        await asyncio.sleep(CHECK_INTERVAL)

def main():
    # Корректное завершение
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_DFL)
    asyncio.run(run())

if __name__ == "__main__":
    main()

