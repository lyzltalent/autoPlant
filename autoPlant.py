import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo  # Python 3.9+
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ====== 全局与日志 ======
TZ = ZoneInfo("Asia/Singapore")
t0 = time.perf_counter()
def _ts():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
def _dt():
    return f"+{int((time.perf_counter() - t0)*1000)}ms"
def log(msg: str):
    print(f"[{_ts()} {_dt()}] {msg}", flush=True)

# ====== 站点配置（按需修改）======
URL = "https://si-qi.xyz/plant_game.php"         # 农场页面
COOKIE_NAME = os.getenv("SIQI_COOKIE_NAME", "c_secure_pass")
COOKIE_DOMAIN = os.getenv("SIQI_COOKIE_DOMAIN", "si-qi.xyz")
COOKIE_FILE_PATH = Path(
    os.getenv(
        "PLANT_COOKIE_FILE",
        os.getenv(
            "SIQI_COOKIE_FILE",
            os.path.join(Path(__file__).resolve().parent, "data", "cookie.txt"),
        ),
    )
)

# ====== 农场页面元素（按页面实际改）======
SEL_FARM_ROOT      = "#lands-list"
SEL_PLOTS_ALL      = ".p-plot"               # 全部地块
SEL_PLOTS_PLANTED  = ".p-plot.planted"       # 已种植地块（含 data-harvest-time）
SEL_SEEDS_ROOT     = "#seeds-list"           # 种子面板（若一直可见也 OK）
SEL_SEED_CARD      = ".p-seed"               # 种子卡片
PLANT_CONFIRM_BTNS = [
    "button:has-text('确认')",
    "button:has-text('种植')",
    "button:has-text('确定')",
]

# ====== 想种的作物（优先番茄/西红柿，没解锁回退萝卜）======
SEED_PREFERRED_TEXTS = ["西红柿", "🍅"]
SEED_FALLBACK_TEXTS  = ["萝卜", "🥕"]

# ====== 调度与行为参数 ======
SMART_SCHEDULE = True            # True: 按最早成熟时间唤醒；False: 固定间隔
FIXED_INTERVAL_SEC = 180
SAFETY_BUFFER_SEC = -5
MIN_WAKE_INTERVAL_SEC = 10
FALLBACK_INTERVAL_SEC = 300
HEADLESS = os.getenv("PLANT_HEADLESS", os.getenv("HEADLESS", "true")).lower() != "false"
OVERRIDE_CONFIRM = True         # 若页面使用原生 window.confirm，置 True 可直接短路为接受


# ====== 通用 ======
def ensure_cookie_value() -> str:
    env_cookie = (os.getenv("PLANT_COOKIE") or os.getenv("SIQI_COOKIE") or "").strip()
    if env_cookie:
        return env_cookie
    if COOKIE_FILE_PATH.exists():
        value = COOKIE_FILE_PATH.read_text(encoding="utf-8").strip()
        if value:
            return value
    raise RuntimeError(
        f"未找到 cookie。请在管理页面填写 {COOKIE_NAME}，或设置 SIQI_COOKIE/PLANT_COOKIE 环境变量。"
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


# —— 新增小工具：判断指定地块是否已处于“已种植(.planted)”状态
def is_planted_selector(page, plot_selector: str) -> bool:
    try:
        return bool(page.locator(plot_selector).evaluate("el => el.classList.contains('planted')"))
    except Exception:
        return False


def fmt_epoch(ts_sec: int) -> str:
    """将秒级时间戳按脚本时区格式化成人类可读时间"""
    try:
        return datetime.fromtimestamp(int(ts_sec), TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts_sec)


def open_target(page):
    backoff = 1.0
    while True:
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=45_000)
            return
        except Exception as e:
            log(f"[open] load failed, retry in {backoff:.1f}s: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 1.8, 10)

# ====== 工具 ======
def plot_key_selector(el) -> str:
    land = el.get_attribute("data-land") or ""
    idx  = el.get_attribute("data-plot") or ""
    return f'.p-plot[data-land="{land}"][data-plot="{idx}"]'

def is_mature(el) -> bool:
    try:
        t = el.get_attribute("data-harvest-time")
        return bool(t) and int(t) <= int(time.time())
    except Exception:
        return False

def get_next_harvest_ts(page) -> int:
    now = int(time.time())
    try:
        times = page.eval_on_selector_all(
            SEL_PLOTS_PLANTED,
            "els => els.map(e => Number(e.getAttribute('data-harvest-time')||'0'))"
        )
    except Exception as e:
        log(f"[plan] 读取 plots 失败: {e}")
        times = []
    future = [t for t in times if t > now]
    return min(future) if future else 0

# ====== 收获：逐块点击成熟地块 ======
def harvest_mature_plots(page):
    planted = page.locator(SEL_PLOTS_PLANTED)
    n = planted.count()
    mature_selectors = []
    for i in range(n):
        el = planted.nth(i)
        if is_mature(el):
            mature_selectors.append(plot_key_selector(el))

    if not mature_selectors:
        log("[harvest] 没有成熟地块。")
        return 0

    log(f"[harvest] 成熟地块数量: {len(mature_selectors)}")
    for idx, sel in enumerate(mature_selectors, 1):
        try:
            page.locator(sel).click(timeout=4000)
            log(f"[harvest] {idx}/{len(mature_selectors)} -> confirm accept")
        except Exception as e:
            log(f"[harvest] {idx} 异常: {e}")
        page.wait_for_timeout(200)
    # 等网络与 DOM 稳定
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)
    return len(mature_selectors)

# ====== 选种：优先番茄/西红柿，未解锁回退萝卜 ======
def pick_seed_locator(page):
    # 只在未锁定的卡片中找
    for t in SEED_PREFERRED_TEXTS:
        loc = page.locator(f"{SEL_SEED_CARD}:not(.locked)").filter(has_text=t)
        if loc.count() > 0:
            return loc.first, f"preferred:{t}"
    for t in SEED_FALLBACK_TEXTS:
        loc = page.locator(f"{SEL_SEED_CARD}:not(.locked)").filter(has_text=t)
        if loc.count() > 0:
            return loc.first, f"fallback:{t}"
    return None, "none"


def confirm_plant_if_needed(page):
    for sel in PLANT_CONFIRM_BTNS:
        btn = page.locator(sel)
        if btn.count() > 0 and btn.first.is_visible():
            try:
                btn.first.click(timeout=2000)
                return True
            except Exception:
                pass
    return False


# —— 替换原来的 plant_on_all_empty_slots：先点一次种子，再逐块点空地
def plant_on_all_empty_slots(page):
    # 1) 空地 = 不是已种(.planted)，也不是购买位/未解锁位
    empty = page.locator(
        f'{SEL_PLOTS_ALL}:not(.planted):not(.slot-buyable):not(.slot-locked)'
    )

    cnt = empty.count()
    if cnt <= 0:
        log("[plant] 无空地，无需补种。")
        return 0

    empty_keys = []
    for i in range(cnt):
        el = empty.nth(i)
        empty_keys.append(plot_key_selector(el))  # e.g. .p-plot[data-land="1"][data-plot="3"]

    # 2) 选择种子：优先番茄/西红柿，未解锁则回退萝卜
    seed_loc, which = pick_seed_locator(page)
    if seed_loc is None:
        log("❌ 没有可用的种子（番茄未解锁且无可用回退）。")
        return 0

    try:
        page.wait_for_selector(SEL_SEEDS_ROOT, state="visible", timeout=2000)
    except PlaywrightTimeoutError:
        pass

    try:
        seed_loc.scroll_into_view_if_needed()
    except Exception:
        pass

    # 3) 进入“种植模式”：先点一次种子（如页面在点空地时才弹原生 confirm，也没关系，下面都兜住了）
    try:
        seed_loc.click(timeout=2000)
        log(f"[plant] 进入种植模式（点击种子成功）")
    except Exception as e:
        log(f"[plant] 点击种子失败：{e}")
        return 0

    # 4) 逐块点击空地进行种植；若失败自动重选种子重试一次
    planted_ok = 0
    log(f"[plant] 空地 {len(empty_keys)} 块，开始按“种子一次 + 多地块”逻辑种植（{which}）…")

    for idx, sel in enumerate(empty_keys, 1):
        # 如果该地块在过程中已被种上（并发/上一次循环刚种上），就跳过
        if is_planted_selector(page, sel):
            log(f"[plant] {idx}/{len(empty_keys)} 已是 planted，跳过。")
            continue

        def click_plot_once():
            try:
                page.locator(sel).click(timeout=2000)
                log(f"[plant] 进入种植模式（点击空地成功）")
                page.wait_for_timeout(150)  # 给 DOM 一口气
                return True
            except Exception as e:
                log(f"[plant] 点击空地失败（{sel}）：{e}")
                return False

        # 第一次尝试
        ok = click_plot_once()

        if ok:
            planted_ok += 1
            log(f"[plant] {idx}/{len(empty_keys)} ✅ done")
        else:
            log(f"[plant] {idx}/{len(empty_keys)} ❌ 未能种上（已重试）")

        page.wait_for_timeout(150)

    # 5) 落定
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(300)
    return planted_ok

# ====== 单次执行：收获成熟 → 种番茄（或回退） → 计算下次时间 ======
def run_once():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )

        storage_state = build_storage_state()
        log("🔐 使用当前 cookie 登录…")
        context = browser.new_context(
            storage_state=storage_state,
            viewport={"width": 1280, "height": 800},
        )

        if OVERRIDE_CONFIRM:
            context.add_init_script("() => { window.confirm = () => true; }")

        page = context.new_page()
        open_target(page)
        try:
            page.wait_for_selector(SEL_FARM_ROOT, timeout=20_000)
        except PlaywrightTimeoutError:
            log("⚠️ 农场根元素未出现，继续尝试…")

        harvested = harvest_mature_plots(page)
        planted = plant_on_all_empty_slots(page)

        next_ts = get_next_harvest_ts(page)
        browser.close()

    # 调度
    now_ms = int(time.time() * 1000)
    if next_ts and next_ts * 1000 > now_ms:
        delay_ms = (next_ts * 1000) - now_ms - SAFETY_BUFFER_SEC * 1000
        delay_ms = max(delay_ms, MIN_WAKE_INTERVAL_SEC * 1000)
        next_run_epoch = time.time() + delay_ms / 1000.0
    else:
        delay_ms = FALLBACK_INTERVAL_SEC * 1000
        next_run_epoch = time.time() + delay_ms / 1000.0

    wait_sec = int(delay_ms / 1000)
    next_run_str = fmt_epoch(next_run_epoch)

    log(f"[summary] 本次收获 {harvested} 块，补种 {planted} 块。")
    log(f"[scheduler] 下一次建议在 {next_run_str} 运行（约 {wait_sec}s 后）")
    return wait_sec

# ====== 调度器 ======
def loop_smart():
    while True:
        try:
            wait_sec = run_once()
        except Exception as e:
            log(f"[loop] run_once 异常：{e}")
            wait_sec = 60
        time.sleep(wait_sec)

def loop_fixed():
    interval = max(5, int(FIXED_INTERVAL_SEC))
    while True:
        try:
            run_once()
        except Exception as e:
            log(f"[loop-fixed] 异常：{e}")
        time.sleep(interval)

if __name__ == "__main__":
    log("🚀 农场：逐块收获 + 直接点种子补种（番茄优先）启动…")
    if SMART_SCHEDULE:
        loop_smart()
    else:
        loop_fixed()