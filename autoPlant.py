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

# ====== 想种的作物 ======
SEED_PREFERRED_TEXTS = ["茄子", "🍆"]
SEED_FALLBACK_TEXTS  = ["玉米", "🌽"]

# ====== 调度与行为参数 ======
SMART_SCHEDULE = True            # True: 按最早成熟时间唤醒；False: 固定间隔
FIXED_INTERVAL_SEC = 180
SAFETY_BUFFER_SEC = -5
MIN_WAKE_INTERVAL_SEC = 10
FALLBACK_INTERVAL_SEC = 300
HEADLESS = os.getenv("PLANT_HEADLESS", os.getenv("HEADLESS", "true")).lower() != "false"
OVERRIDE_CONFIRM = True         # 若页面使用原生 window.confirm，置 True 可直接短路为接受

# ====== 出售配置 ======
SELL_AFTER_HARVEST = True        # 收获后自动出售背包全部作物


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


# —— 新增小工具：判断指定地块是否已处于"已种植(.planted)"状态
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

# ====== 出售背包作物 ======
def sell_one_crop(page, seed_id: int, quantity: int = None):
    """
    出售指定 seed_id 的作物。
    - 若 quantity 为 None，则取输入框的 max 值（全部出售）。
    返回出售的份数，失败返回 0。
    """
    input_sel = f'input[data-seed-input="{seed_id}"]'
    btn_sel  = f'button[data-action="sell"][data-seed-id="{seed_id}"]'

    # 检查元素是否存在
    input_el = page.locator(input_sel)
    btn_el  = page.locator(btn_sel)
    if input_el.count() == 0 or btn_el.count() == 0:
        log(f"[sell] seed_id={seed_id} 的输入框或按钮不存在，跳过。")
        return 0

    # 确定数量
    if quantity is None:
        try:
            max_str = input_el.get_attribute("max")
            quantity = int(max_str) if max_str else 1
        except (ValueError, TypeError):
            quantity = 1

    if quantity <= 0:
        log(f"[sell] seed_id={seed_id} 数量为 0，跳过。")
        return 0

    try:
        # 用 JS 设置输入值并触发 input/change 事件（比 fill() 更可靠）
        page.evaluate(
            f"""() => {{
                const inp = document.querySelector('{input_sel}');
                if (!inp) return;
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                nativeSetter.call(inp, '{quantity}');
                inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }}"""
        )
        page.wait_for_timeout(150)

        # 滚动到按钮位置再点"售出"
        btn_el.scroll_into_view_if_needed()
        page.wait_for_timeout(100)
        btn_el.click(timeout=3000)
        log(f"[sell] 点击售出：seed_id={seed_id}, 数量={quantity}")

        # 弹窗确认：点"确认售出"按钮
        page.wait_for_timeout(300)
        try:
            ok_btn = page.locator("#sell-ok-btn")
            if ok_btn.count() > 0 and ok_btn.first.is_visible():
                ok_btn.first.click(timeout=3000)
                log(f"[sell] 点击弹窗确认售出")
            else:
                # 备选：找文本包含"确认售出"的按钮
                alt_btn = page.locator("button:has-text('确认售出')")
                if alt_btn.count() > 0 and alt_btn.first.is_visible():
                    alt_btn.first.click(timeout=3000)
                    log(f"[sell] 点击弹窗确认售出(备选)")
        except Exception as e:
            log(f"[sell] 确认弹窗点击异常(可能无弹窗): {e}")

        # 等网络稳定
        page.wait_for_timeout(500)

        return quantity
    except Exception as e:
        log(f"[sell] seed_id={seed_id} 出售失败: {e}")
        return 0


def sell_all_inventory(page):
    """
    遍历背包，将所有作物全部出售。
    返回 (总出售种类数, 总售出份数)。
    """
    # 找到所有出售按钮，每个按钮对应一种作物
    sell_btns = page.locator('button[data-action="sell"]')
    count = sell_btns.count()
    if count == 0:
        log("[sell] 背包为空，无需出售。")
        return (0, 0)

    total_kinds = 0
    total_qty   = 0

    for i in range(count):
        btn = sell_btns.nth(i)
        try:
            seed_id_str = btn.get_attribute("data-seed-id")
            if not seed_id_str:
                continue
            seed_id = int(seed_id_str)

            # 获取作物名称用于日志
            # 向上找到 .p-inventory-item，再找 .p-inventory-name
            name = ""
            try:
                item_el = btn.locator("xpath=ancestor::div[contains(@class, 'p-inventory-item')]")
                name_el = item_el.locator(".p-inventory-name")
                if name_el.count() > 0:
                    name = name_el.first.inner_text()
            except Exception:
                pass

            qty = sell_one_crop(page, seed_id)
            if qty > 0:
                total_kinds += 1
                total_qty   += qty
                log(f"[sell] 已出售 {name or f'seed_id={seed_id}'} x{qty}")
            page.wait_for_timeout(200)
        except Exception as e:
            log(f"[sell] 第 {i} 个物品出售异常: {e}")

    if total_kinds > 0:
        log(f"[sell] 本次共出售 {total_kinds} 种作物，合计 {total_qty} 份。")
    else:
        log("[sell] 没有可出售的作物。")
    return (total_kinds, total_qty)


# ====== 选种：优先茄子，未解锁回退玉米 ======
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
        empty_keys.append(plot_key_selector(el))

    # 2) 选择种子
    seed_loc, which = pick_seed_locator(page)
    if seed_loc is None:
        log("❌ 没有可用的种子。")
        return 0

    try:
        page.wait_for_selector(SEL_SEEDS_ROOT, state="visible", timeout=2000)
    except PlaywrightTimeoutError:
        pass

    try:
        seed_loc.scroll_into_view_if_needed()
    except Exception:
        pass

    # 3) 进入种植模式：先点一次种子
    try:
        seed_loc.click(timeout=2000)
        log(f"[plant] 进入种植模式（点击种子成功）")
    except Exception as e:
        log(f"[plant] 点击种子失败：{e}")
        return 0

    # 4) 逐块点击空地进行种植
    planted_ok = 0
    log(f"[plant] 空地 {len(empty_keys)} 块，开始种植（{which}）…")

    for idx, sel in enumerate(empty_keys, 1):
        if is_planted_selector(page, sel):
            log(f"[plant] {idx}/{len(empty_keys)} 已是 planted，跳过。")
            continue

        def click_plot_once():
            try:
                page.locator(sel).click(timeout=2000)
                page.wait_for_timeout(150)
                return True
            except Exception as e:
                log(f"[plant] 点击空地失败（{sel}）：{e}")
                return False

        ok = click_plot_once()
        if ok:
            planted_ok += 1
            log(f"[plant] {idx}/{len(empty_keys)} ✅ done")
        else:
            log(f"[plant] {idx}/{len(empty_keys)} ❌ 未能种上")

        page.wait_for_timeout(150)

    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(300)
    return planted_ok

# ====== 单次执行：收获 → 出售 → 补种 → 计算下次时间 ======
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

        # —— 收获后自动出售 ——
        if SELL_AFTER_HARVEST:
            log("[sell] 收获完成，开始出售背包作物…")
            try:
                sold_kinds, sold_qty = sell_all_inventory(page)
                log(f"[sell] 出售完成：{sold_kinds} 种，{sold_qty} 份")
            except Exception as e:
                log(f"[sell] 出售过程异常: {e}")

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

# ====== 测试入口：单独出售指定 seed_id（跑一次就退出）======
def run_sell_test(seed_id: int = 4, quantity: int = None):
    """单独测试出售功能：只打开页面出售指定作物，不收获不补种"""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        storage_state = build_storage_state()
        context = browser.new_context(
            storage_state=storage_state,
            viewport={"width": 1280, "height": 800},
        )
        if OVERRIDE_CONFIRM:
            context.add_init_script("() => { window.confirm = () => true; }")
        page = context.new_page()
        open_target(page)

        qty = sell_one_crop(page, seed_id, quantity)
        log(f"[test] 出售 seed_id={seed_id}, 数量={qty if qty else '失败'}")
        browser.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "sell-test":
        # python plant.py sell-test [seed_id] [quantity]
        sid = int(sys.argv[2]) if len(sys.argv) > 2 else 4
        q   = int(sys.argv[3]) if len(sys.argv) > 3 else None
        log(f"🧪 单独出售测试：seed_id={sid}, quantity={q or '全部'}")
        run_sell_test(sid, q)
    else:
        log("🚀 农场：收获 + 自动出售 + 补种启动…")
        if SMART_SCHEDULE:
            loop_smart()
        else:
            loop_fixed()
