import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo  # Python 3.9+
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# 固定为新加坡时间；如果想用本机时区，去掉 ZoneInfo(...)
TZ = ZoneInfo("Asia/Singapore")

t0 = time.perf_counter()


def _ts():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _dt():
    return f"+{int((time.perf_counter() - t0)*1000)}ms"


def log(msg: str):
    print(f"[{_ts()} {_dt()}] {msg}", flush=True)


# ====== 站点配置（按需修改）======
URL = "https://si-qi.xyz/mowan.php"
COOKIE_NAME = os.getenv("SIQI_COOKIE_NAME", "c_secure_pass")
COOKIE_DOMAIN = os.getenv("SIQI_COOKIE_DOMAIN", "si-qi.xyz")
COOKIE_FILE_PATH = Path(
    os.getenv(
        "MOWAN_COOKIE_FILE",
        os.getenv(
            "SIQI_COOKIE_FILE",
            os.path.join(Path(__file__).resolve().parent, "data", "cookie.txt"),
        ),
    )
)

# ====== 清理参数（可调）======
CHECK_INTERVAL_MS = 3000
ITEM_CLICK_INTERVAL_MS = 200
STATUS_RECHECK_INTERVAL_SEC = int(os.getenv("MOWAN_STATUS_RECHECK_SEC", str(3600)))
CLEAN_SESSION_SEC = int(os.getenv("MOWAN_SESSION_SEC", str(600)))  # 默认清理 10 分钟
DEFAULT_NEXT_INTERVAL_SEC = int(os.getenv("MOWAN_DEFAULT_NEXT_SEC", str(2 * 3600)))
BRICK_DEFAULT_INTERVAL_SEC = int(
    os.getenv("MOWAN_BRICK_DEFAULT_SEC", str(24 * 3600))
)
BRICK_RECHECK_INTERVAL_SEC = int(
    os.getenv("MOWAN_BRICK_RECHECK_SEC", str(6 * 3600))
)
BRICK_CLICK_COUNT = int(os.getenv("MOWAN_BRICK_CLICKS", "50"))
BRICK_CLICK_INTERVAL_MS = int(os.getenv("MOWAN_BRICK_CLICK_MS", "150"))
MIN_WAIT_AFTER_CHECK_SEC = 30

# ====== 页面元素选择器（按页面实际改）======
SEL_CLEAN_BTN = "#beachBtn"
SEL_BEACH_AREA = "#beachArea"
SEL_DROP_ITEMS = "#beachArea > *"
SEL_STATUS_AREA = "#beachStatus"
SEL_BRICK_FACTORY = "#brickFactory"
SEL_BRICK_STATUS_AREA = "#brickStatus"

COUNTDOWN_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")


def parse_countdown_text(text: str | None) -> int | None:
    if not text:
        return None
    text = text.strip()
    match = COUNTDOWN_RE.search(text)
    if match:
        h, m, s = map(int, match.groups())
        return h * 3600 + m * 60 + s
    digits = re.findall(r"\d+", text)
    if len(digits) >= 3:
        h, m, s = map(int, digits[:3])
        return h * 3600 + m * 60 + s
    return None


def open_target(page):
    """进入目标页，带退避重试"""
    backoff = 1.0
    while True:
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=45_000)
            return
        except Exception as e:
            log(f"[open] load failed, retry in {backoff:.1f}s: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 1.8, 10)


def ensure_cookie_value() -> str:
    env_cookie = (os.getenv("MOWAN_COOKIE") or os.getenv("SIQI_COOKIE") or "").strip()
    if env_cookie:
        return env_cookie
    if COOKIE_FILE_PATH.exists():
        value = COOKIE_FILE_PATH.read_text(encoding="utf-8").strip()
        if value:
            return value
    raise RuntimeError(
        f"未找到 cookie。请在管理页面填写 {COOKIE_NAME}，或设置 SIQI_COOKIE/MOWAN_COOKIE 环境变量。"
    )


def build_storage_state() -> dict:
    cookie_value = ensure_cookie_value()
    return {
        "cookies": [
            {
                "name": COOKIE_NAME,
                "value": cookie_value,
                "domain": COOKIE_DOMAIN,
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


def click_clean_button(page):
    btn = page.locator(SEL_CLEAN_BTN)
    if btn.count() == 0:
        return
    try:
        if not btn.is_visible():
            return
        disabled_attr = btn.get_attribute("disabled")
        aria_disabled = btn.get_attribute("aria-disabled")
        if disabled_attr is None and aria_disabled != "true":
            try:
                btn.scroll_into_view_if_needed()
            except Exception:
                pass
            try:
                btn.click(timeout=3000)
            except Exception as e:
                log(f"[click clean] failed: {e}")
    except Exception as e:
        log(f"[btn state] err: {e}")


def click_drops(page):
    items = page.locator(SEL_DROP_ITEMS)
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except PlaywrightTimeoutError:
        log("[drops] wait_for_load_state timeout, continue anyway")
    except Exception as e:
        log(f"[drops] wait_for_load_state err: {e}")
    page.wait_for_timeout(200)
    try:
        count = items.count()
    except Exception:
        count = 0

    if count <= 0:
        return

    for i in range(count):
        el = items.first
        try:
            txt = el.inner_text(timeout=500)
        except Exception:
            txt = ''
        try:
            try:
                el.scroll_into_view_if_needed()
            except Exception:
                pass
            try:
                el.click(timeout=1500)
                log(f"[click item {i+1}/{count}] ✅ success  [{txt.strip()}]")
                time.sleep(1)
            except PlaywrightTimeoutError as e:
                log(f"[click item {i+1}/{count}] timeout: {e}")
            except Exception as e:
                log(f"[click item {i+1}/{count}] err: {e}")
        except Exception as e:
            log(f"[item {i+1}] unexpected: {e}")
        page.wait_for_timeout(ITEM_CLICK_INTERVAL_MS)


# ── 从页面全文搜索倒计时的兜底函数 ──
def _search_countdown_in_page(page, *, skip_first: bool = False) -> int | None:
    """
    从整个页面文本中搜索 HH:MM:SS 格式的倒计时。
    skip_first=True 时跳过第一个匹配（用于砖场，跳过沙滩的倒计时）。
    """
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
        matches = COUNTDOWN_RE.findall(body_text)
        if matches:
            idx = 1 if skip_first and len(matches) >= 2 else 0
            if idx < len(matches):
                h, m, s = map(int, matches[idx])
                seconds = h * 3600 + m * 60 + s
                tag = "2nd" if skip_first else "1st"
                log(f"[fallback] found {tag} countdown in page text: {h:02d}:{m:02d}:{s:02d} => {seconds}s")
                return seconds
    except Exception as e:
        log(f"[fallback] page text search err: {e}")
    return None


def read_countdown_seconds(page, *, timeout_ms: int = 10000) -> int | None:
    """读取沙滩清理倒计时，多重降级"""
    # 方法 1：读 #beachStatus 整个容器的文本（不依赖 .countdown 子元素）
    try:
        status_el = page.locator(SEL_STATUS_AREA)
        status_el.wait_for(state="attached", timeout=timeout_ms)
        text = status_el.inner_text(timeout=3000)
        seconds = parse_countdown_text(text)
        if seconds is not None:
            log(f"[status] beachStatus text='{text.strip()}' => {seconds}s")
            return seconds
        else:
            log(f"[status] no countdown in beachStatus, text='{text.strip()}'")
    except PlaywrightTimeoutError:
        log("[status] #beachStatus not found within timeout")
    except Exception as e:
        log(f"[status] #beachStatus read err: {e}")

    # 方法 2：尝试 .countdown 子元素
    try:
        countdown = page.locator(f"{SEL_STATUS_AREA} .countdown")
        if countdown.count() > 0:
            text = countdown.inner_text(timeout=3000)
            seconds = parse_countdown_text(text)
            log(f"[status] .countdown text='{text.strip()}' => {seconds}s")
            return seconds
    except Exception as e:
        log(f"[status] .countdown read err: {e}")

    # 方法 3：全文搜索兜底
    return _search_countdown_in_page(page)


def is_brick_available(page, *, timeout_ms: int = 10000) -> bool:
    """
    判断砖场是否可用（可以搬砖）。
    根据 #brickStatus 文本判断：
    - 包含 "可以搬砖" → True（立即可用）
    - 包含倒计时 → 还在冷却
    - 其他/未找到 → False
    """
    try:
        status_el = page.locator(SEL_BRICK_STATUS_AREA)
        status_el.wait_for(state="attached", timeout=timeout_ms)
        text = status_el.inner_text(timeout=3000)
        text_stripped = text.strip()
        if "可以搬砖" in text_stripped:
            log(f"[brick status] ✅ 砖场可用，text='{text_stripped}'")
            return True
        seconds = parse_countdown_text(text_stripped)
        if seconds is not None:
            log(f"[brick status] 砖场冷却中，text='{text_stripped}' => {seconds}s")
            return False
        log(f"[brick status] 未知状态，text='{text_stripped}'")
        return False
    except PlaywrightTimeoutError:
        log("[brick status] #brickStatus not found within timeout")
    except Exception as e:
        log(f"[brick status] read err: {e}")
    return False


def read_brick_countdown_seconds(page, *, timeout_ms: int = 10000) -> int | None:
    """读取砖场倒计时秒数。如果砖场可用（可以搬砖），返回 0。"""
    try:
        status_el = page.locator(SEL_BRICK_STATUS_AREA)
        status_el.wait_for(state="attached", timeout=timeout_ms)
        text = status_el.inner_text(timeout=3000)
        text_stripped = text.strip()
        if "可以搬砖" in text_stripped:
            log(f"[brick status] ✅ 砖场可用，返回 0，text='{text_stripped}'")
            return 0
        seconds = parse_countdown_text(text_stripped)
        if seconds is not None:
            log(f"[brick status] countdown='{text_stripped}' => {seconds}s")
            return seconds
        log(f"[brick status] no countdown in brickStatus, text='{text_stripped}'")
    except PlaywrightTimeoutError:
        log("[brick status] #brickStatus not found within timeout")
    except Exception as e:
        log(f"[brick status] read err: {e}")

    # 方法 2：尝试 .countdown 子元素（可能页面更新后出现）
    try:
        countdown = page.locator(f"{SEL_BRICK_STATUS_AREA} .countdown")
        if countdown.count() > 0:
            text = countdown.inner_text(timeout=3000)
            seconds = parse_countdown_text(text)
            if seconds is not None:
                log(f"[brick status] .countdown text='{text.strip()}' => {seconds}s")
                return seconds
    except Exception as e:
        log(f"[brick status] .countdown read err: {e}")

    # 不再全文搜索兜底——页面上其他倒计时（如全站Free）会干扰
    log("[brick status] 无法确定砖场状态，返回 None")
    return None


def wait_for_brick_factory_ready(page, timeout_sec: int = 30) -> bool:
    factory = page.locator(SEL_BRICK_FACTORY)
    try:
        factory.wait_for(state="attached", timeout=timeout_sec * 1000)
    except PlaywrightTimeoutError:
        log("[brick] factory element missing")
        return False

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            ready = factory.evaluate(
                "el => window.getComputedStyle(el).pointerEvents !== 'none'"
            )
        except Exception:
            ready = False
        if ready:
            return True
        time.sleep(1)
    log("[brick] factory still locked after waiting")
    return False


def click_brick_factory(page, clicks: int = BRICK_CLICK_COUNT):
    factory = page.locator(SEL_BRICK_FACTORY)
    success = 0
    for i in range(clicks):
        try:
            factory.click(timeout=3000)
            success += 1
            log(f"[brick] click {i+1}/{clicks} ✅")
        except PlaywrightTimeoutError as e:
            log(f"[brick] click {i+1} timeout: {e}")
            break
        except Exception as e:
            log(f"[brick] click {i+1} unexpected: {e}")
            break
        page.wait_for_timeout(BRICK_CLICK_INTERVAL_MS)
    log(f"[brick] 完成 {success}/{clicks} 次点击")
    return success


def open_page_session(playwright_obj):
    headless = True
    browser = playwright_obj.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )

    storage_state = build_storage_state()
    log("🔐 使用当前 cookie 登录…")
    context = browser.new_context(
        storage_state=storage_state,
        viewport={"width": 1280, "height": 800},
    )

    page = context.new_page()
    open_target(page)

    try:
        page.wait_for_selector(SEL_BEACH_AREA, timeout=20_000)
    except PlaywrightTimeoutError:
        pass

    return browser, page


def run_loop(page, *, max_runtime_sec: int | None = None):
    """自动清理主循环"""
    start = time.time()
    try:
        while True:
            click_clean_button(page)
            click_drops(page)
            page.wait_for_timeout(CHECK_INTERVAL_MS)

            if not page.url.startswith(URL):
                log("[watchdog] url changed, navigating back...")
                open_target(page)

            if max_runtime_sec and (time.time() - start) >= max_runtime_sec:
                log("[run_loop] reach session runtime limit, exiting loop")
                break
    finally:
        pass


def normalize_wait_seconds(seconds: int | None, reason: str) -> int:
    return _normalize_wait(seconds, reason, DEFAULT_NEXT_INTERVAL_SEC)


def _normalize_wait(seconds: int | None, reason: str, fallback: int) -> int:
    if seconds is None:
        log(f"[schedule] {reason}: countdown missing，fallback {fallback}s")
        seconds = fallback
    seconds = max(seconds, MIN_WAIT_AFTER_CHECK_SEC)
    eta = datetime.now(TZ) + timedelta(seconds=seconds)
    log(
        f"[schedule] {reason}: wait {int(seconds)}s, next target at {eta.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    return int(seconds)


def fetch_countdown_wait(reason: str) -> int:
    with sync_playwright() as p:
        browser, page = open_page_session(p)
        seconds = read_countdown_seconds(page)
        browser.close()
    return normalize_wait_seconds(seconds, reason)


def fetch_brick_countdown_wait(reason: str) -> int:
    with sync_playwright() as p:
        browser, page = open_page_session(p)
        seconds = read_brick_countdown_seconds(page)
        browser.close()
    return _normalize_wait(seconds, reason, BRICK_DEFAULT_INTERVAL_SEC)


def run_cleaning_session() -> tuple[int, int | None]:
    with sync_playwright() as p:
        browser, page = open_page_session(p)
        log("🚀 开始自动清理会话...")
        run_loop(page, max_runtime_sec=CLEAN_SESSION_SEC)
        next_seconds = read_countdown_seconds(page)
        brick_seconds = read_brick_countdown_seconds(page)
        browser.close()
        log("✅ 清理会话完成，浏览器已关闭")
    brick_wait = (
        _normalize_wait(brick_seconds, "同步砖场倒计时", BRICK_DEFAULT_INTERVAL_SEC)
        if brick_seconds is not None
        else None
    )
    return normalize_wait_seconds(next_seconds, "post-clean countdown"), brick_wait


def run_brick_session() -> tuple[int, int | None]:
    with sync_playwright() as p:
        browser, page = open_page_session(p)
        log("🧱 搬砖工坊检查中…")
        if wait_for_brick_factory_ready(page):
            click_brick_factory(page)
        else:
            log("[brick] 工坊未解锁，跳过点击")
        next_seconds = read_brick_countdown_seconds(page)
        clean_seconds = read_countdown_seconds(page)
        browser.close()
        log("🧱 搬砖流程完成，浏览器已关闭")
    clean_wait = (
        normalize_wait_seconds(clean_seconds, "同步清理倒计时")
        if clean_seconds is not None
        else None
    )
    return _normalize_wait(next_seconds, "post-brick countdown", BRICK_DEFAULT_INTERVAL_SEC), clean_wait


def scheduler_loop():
    log("⏱ 初始化，读取下次清理/搬砖倒计时...")
    clean_wait = fetch_countdown_wait("initial cleaning countdown")
    brick_wait = fetch_brick_countdown_wait("initial brick countdown")
    next_clean_at = time.time() + clean_wait
    next_brick_at = time.time() + brick_wait

    while True:
        now = time.time()
        if next_clean_at <= next_brick_at:
            task = "clean"
            remaining = next_clean_at - now
        else:
            task = "brick"
            remaining = next_brick_at - now

        if remaining <= 0:
            if task == "clean":
                clean_wait, maybe_brick = run_cleaning_session()
                next_clean_at = time.time() + clean_wait
                if maybe_brick is not None:
                    next_brick_at = time.time() + maybe_brick
            else:
                brick_wait, maybe_clean = run_brick_session()
                next_brick_at = time.time() + brick_wait
                if maybe_clean is not None:
                    next_clean_at = time.time() + maybe_clean
            continue

        recheck_interval = (
            STATUS_RECHECK_INTERVAL_SEC if task == "clean" else BRICK_RECHECK_INTERVAL_SEC
        )

        if remaining > recheck_interval:
            log(
                f"[scheduler] next {task} in {int(remaining)}s; sleep {recheck_interval}s then recheck"
            )
            time.sleep(recheck_interval)
            if task == "clean":
                clean_wait = fetch_countdown_wait("hourly recheck")
                next_clean_at = time.time() + clean_wait
            else:
                brick_wait = fetch_brick_countdown_wait("brick recheck")
                next_brick_at = time.time() + brick_wait
        else:
            log(
                f"[scheduler] waiting {int(max(remaining, 1))}s for the next {task} window..."
            )
            time.sleep(max(remaining, 1))


if __name__ == "__main__":
    scheduler_loop()
