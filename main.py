"""
BIST Grafik Görsel Getirme Ajanı  v2.0
===============================================
TradingView üzerinden otomatik 5dk BIST mum grafik screenshot servisi.

Akış:
  GET /prepare-chart  → session_id
  GET /capture-chart  → screenshot_url
  GET /chart-agent    → tek çağrıda ikisi birden (debug)
  GET /health
  GET /debug/range-target
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

# ══════════════════════════════════════════════════════════════════════════════
#  Sabitler
# ══════════════════════════════════════════════════════════════════════════════
TZ_IST         = pytz.timezone("Europe/Istanbul")
BIST_START     = (9, 55)     # seans başlangıcı
BIST_END       = (18, 10)    # seans sonu
SESSION_TTL    = 300         # saniye – hazırlanmış sayfanın ömrü
VIEWPORT       = {"width": 1440, "height": 760}
SCREENSHOT_DIR = Path("/tmp/screenshots")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

# Render, RENDER_EXTERNAL_URL'yi otomatik set eder — ek ayar gerekmez.
# Manuel override için HOST_URL env var kullanılabilir.
HOST_URL = (
    os.getenv("HOST_URL")
    or os.getenv("RENDER_EXTERNAL_URL", "")
).rstrip("/")

# ══════════════════════════════════════════════════════════════════════════════
#  Global tarayıcı + oturum deposu
# ══════════════════════════════════════════════════════════════════════════════
_pw       = None
_browser: Optional[Browser] = None
_sessions: dict[str, dict]  = {}   # session_id → {page, ctx, meta, created_at}


async def _cleanup_loop() -> None:
    """Süresi dolan oturumları arka planda temizle."""
    while True:
        await asyncio.sleep(60)
        now  = time.time()
        dead = [k for k, v in list(_sessions.items())
                if now - v["created_at"] > SESSION_TTL]
        for sid in dead:
            s = _sessions.pop(sid, {})
            for obj in (s.get("page"), s.get("ctx")):
                try:
                    if obj:
                        await obj.close()
                except Exception:
                    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pw, _browser
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-first-run",
            "--single-process",
            "--disable-extensions",
            "--mute-audio",
            "--ignore-certificate-errors",
        ],
    )
    asyncio.create_task(_cleanup_loop())
    yield
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()


# ══════════════════════════════════════════════════════════════════════════════
#  Uygulama
# ══════════════════════════════════════════════════════════════════════════════
app = FastAPI(
    title       = "BIST Grafik Ajanı",
    description = "TradingView BIST 5dk mum grafikleri – otomatik screenshot servisi",
    version     = "2.0.0",
    lifespan    = lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)
app.mount(
    "/screenshots",
    StaticFiles(directory=str(SCREENSHOT_DIR)),
    name="screenshots",
)


# ══════════════════════════════════════════════════════════════════════════════
#  Yardımcı fonksiyonlar
# ══════════════════════════════════════════════════════════════════════════════

def calc_range(target_date: Optional[str]) -> tuple[datetime, datetime]:
    """
    BIST seans aralığını Istanbul saat diliminde hesapla.

    target_date=None → bugün; güncel grafik (09:55 → şu an+1dk, max 18:10)
    target_date=YYYY-MM-DD → o günün tamamı (09:55 → 18:10)
    """
    now = datetime.now(TZ_IST)

    if target_date:
        d     = datetime.strptime(target_date, "%Y-%m-%d")
        start = TZ_IST.localize(datetime(d.year, d.month, d.day, *BIST_START))
        end   = TZ_IST.localize(datetime(d.year, d.month, d.day, *BIST_END))
    else:
        start   = now.replace(hour=BIST_START[0], minute=BIST_START[1],
                               second=0, microsecond=0)
        end_max = now.replace(hour=BIST_END[0],   minute=BIST_END[1],
                               second=0, microsecond=0)
        end_raw = now + timedelta(minutes=1)
        end     = end_raw if end_raw < end_max else end_max

    return start, end


def build_url(symbol: str, start: datetime, end: datetime) -> str:
    """
    TradingView chart URL'si oluştur.
    interval=5 → 5dk mum (zorunlu, değişmez)
    style=1    → mum grafik
    theme=dark → koyu tema (ekran görüntüsü için daha net)
    """
    return (
        f"https://www.tradingview.com/chart/"
        f"?symbol=BIST:{symbol.upper()}"
        f"&interval=5"
        f"&from={int(start.timestamp())}"
        f"&to={int(end.timestamp())}"
        f"&theme=dark"
        f"&style=1"
        f"&hide_side_toolbar=0"
        f"&save_image=false"
    )


def get_base(request: Request) -> str:
    """Sunucunun public base URL'sini döndür (HOST_URL öncelikli)."""
    if HOST_URL:
        return HOST_URL
    return str(request.base_url).rstrip("/")


# ── Tarayıcı otomasyon yardımcıları ──────────────────────────────────────────

async def _dismiss_overlays(page: Page) -> None:
    """Cookie banner ve sign-in popup'larını kapat."""
    # Cookie/GDPR
    for text in ["Accept all", "Accept", "Kabul et", "Agree", "I agree"]:
        try:
            btn = page.locator(f'button:has-text("{text}")').first
            if await btn.is_visible(timeout=800):
                await btn.click()
                await page.wait_for_timeout(400)
                break
        except Exception:
            pass

    # Modal kapatma butonları
    for sel in [
        '[data-name="close"]',
        'button[aria-label="Close"]',
        '.tv-dialog__close',
        '[class*="closeButton"]',
        '[class*="close-button"]',
        '[class*="CloseButton"]',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=500):
                await btn.click()
                await page.wait_for_timeout(300)
        except Exception:
            pass


async def _wait_for_chart(page: Page) -> None:
    """
    Grafik canvas'ının yüklenmesini bekle.
    TradingView JS-ağır olduğu için belirli sinyalleri bekle.
    """
    try:
        await page.wait_for_selector("canvas", timeout=18000)
    except Exception:
        pass  # Canvas bulunamazsa devam et, screenshot zaten gösterecek

    # Yükleme spinner'ı bitene kadar bekle (max 10 sn)
    try:
        await page.wait_for_function(
            "() => !document.querySelector('[class*=\"spinner\"]')",
            timeout=10000,
        )
    except Exception:
        pass

    await page.wait_for_timeout(2500)   # görsel render tamamlansın


async def _enforce_5m(page: Page) -> bool:
    """
    5dk interval'ın aktif olduğunu doğrula.
    URL'de interval=5 zaten ayarlı; bu fonksiyon ek güvenlik katmanıdır.
    Yanlış interval tespitinde True döner (sorun yok).
    """
    try:
        text = await page.evaluate("""
            () => {
                // TradingView üst toolbar'daki aktif interval metnini bul
                const candidates = [
                    '[data-name="header-toolbar-intervals"] [class*="text"]',
                    '[class*="interval"] [class*="active"]',
                    '[class*="toolbar"] [class*="selected"]',
                ];
                for (const sel of candidates) {
                    const el = document.querySelector(sel);
                    if (el) return el.textContent.trim();
                }
                return '';
            }
        """)
        return "5" in str(text)
    except Exception:
        return True   # belirsiz → devam et


async def _hover_last_candle(page: Page) -> None:
    """
    Grafikteki en sağdaki son gerçek mumun üzerine hover yap.
    TradingView header'ında OHLC değerlerinin görünmesini sağlar.

    Strateji:
      - En büyük canvas'ı bul (ana grafik)
      - Fiyat skalasının (sağda ~65px) hemen soluna kon
      - Yavaş hareketle TradingView hover event'ini tetikle
    """
    try:
        await page.wait_for_selector("canvas", timeout=6000)

        # En büyük (ana grafik) canvas'ı bul
        canvases = page.locator("canvas")
        n        = await canvases.count()
        best: Optional[dict] = None

        for i in range(n):
            box = await canvases.nth(i).bounding_box()
            if box and box["width"] > 400 and box["height"] > 200:
                if best is None or (box["width"] * box["height"]
                                    > best["width"] * best["height"]):
                    best = box

        if not best:
            return

        # Hedef koordinatlar
        # x → fiyat skalasının (~65px) hemen solunda = son mum civarı
        # y → grafiğin üst %38'i (mum gövdeleri genellikle burada)
        target_x = best["x"] + best["width"] - 90
        target_y = best["y"] + best["height"] * 0.38

        # Ortadan yavaşça sağa sür
        mid_x = best["x"] + best["width"] * 0.55
        await page.mouse.move(mid_x, target_y)
        await page.wait_for_timeout(150)
        await page.mouse.move(target_x, target_y, steps=20)
        await page.wait_for_timeout(900)

    except Exception:
        pass   # hover başarısız olsa bile screenshot alınabilir


# ══════════════════════════════════════════════════════════════════════════════
#  Çekirdek iş mantığı (endpoint'lerden bağımsız çağrılabilir)
# ══════════════════════════════════════════════════════════════════════════════

async def _do_prepare(symbol: str, target_date: Optional[str]) -> dict:
    """
    TradingView'de 5dk BIST grafiğini hazırla.
    Başarılıysa session_id döner → _do_capture'a verilir.
    """
    if not _browser or not _browser.is_connected():
        return {
            "ok":           False,
            "status":       "prepare_failed",
            "failed_stage": "browser_unavailable",
            "error":        "Tarayıcı hazır değil. /health kontrol et.",
        }

    # Tarih aralığını hesapla
    try:
        start_dt, end_dt = calc_range(target_date)
    except ValueError as exc:
        return {
            "ok":           False,
            "status":       "prepare_failed",
            "failed_stage": "invalid_date",
            "error":        str(exc),
        }

    url = build_url(symbol, start_dt, end_dt)
    sid = str(uuid.uuid4())

    ctx:  Optional[BrowserContext] = None
    page: Optional[Page]           = None

    try:
        ctx = await _browser.new_context(
            viewport       = VIEWPORT,
            user_agent     = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale         = "tr-TR",
            timezone_id    = "Europe/Istanbul",
        )
        page = await ctx.new_page()

        # Sayfa yükle
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3500)

        # Overlay/popup temizle
        await _dismiss_overlays(page)
        await page.wait_for_timeout(2000)

        # Grafik yüklenmesini bekle
        await _wait_for_chart(page)

        # 5dk doğrula
        await _enforce_5m(page)

        # Son mum hover
        await _hover_last_candle(page)

        # Session'ı depola
        _sessions[sid] = {
            "page":       page,
            "ctx":        ctx,
            "symbol":     symbol.upper(),
            "start_str":  start_dt.strftime("%d.%m.%Y %H:%M"),
            "end_str":    end_dt.strftime("%d.%m.%Y %H:%M"),
            "tv_url":     url,
            "created_at": time.time(),
        }

        return {
            "ok":           True,
            "status":       "ready_for_capture",
            "session_id":   sid,
            "symbol":       symbol.upper(),
            "interval":     "5dk",
            "target_start": start_dt.strftime("%d.%m.%Y %H:%M"),
            "target_end":   end_dt.strftime("%d.%m.%Y %H:%M"),
        }

    except Exception as exc:
        for obj in (page, ctx):
            try:
                if obj:
                    await obj.close()
            except Exception:
                pass
        return {
            "ok":           False,
            "status":       "prepare_failed",
            "failed_stage": "page_load",
            "error":        str(exc),
        }


async def _do_capture(session_id: str, req_base: str) -> dict:
    """
    Hazırlanmış sayfanın ekran görüntüsünü al, screenshot_url döndür.
    """
    s = _sessions.get(session_id)
    if not s:
        return {
            "ok":           False,
            "status":       "capture_failed",
            "failed_stage": "session_not_found",
            "error":        "session_id geçersiz veya süresi dolmuş.",
        }

    page: Page = s["page"]
    if page.is_closed():
        _sessions.pop(session_id, None)
        return {
            "ok":           False,
            "status":       "capture_failed",
            "failed_stage": "page_closed",
            "error":        "Sayfa kapanmış. Yeni /prepare-chart çağrısı gerekiyor.",
        }

    try:
        # Son hover tekrarı (hafif kayma olursa düzelt)
        await _hover_last_candle(page)
        await page.wait_for_timeout(500)

        # Dosya adı: THYAO_5m_20260602_173422_a1b2c3d4.png
        fname = (
            f"{s['symbol']}_5m"
            f"_{datetime.now(TZ_IST).strftime('%Y%m%d_%H%M%S')}"
            f"_{session_id[:8]}.png"
        )
        path = SCREENSHOT_DIR / fname
        await page.screenshot(path=str(path), full_page=False)

        # Sayfayı kapat, session'ı temizle
        for obj in (page, s.get("ctx")):
            try:
                if obj:
                    await obj.close()
            except Exception:
                pass
        _sessions.pop(session_id, None)

        return {
            "ok":             True,
            "status":         "captured",
            "screenshot_url": f"{req_base}/screenshots/{fname}",
            "symbol":         s["symbol"],
            "interval":       "5dk",
            "target_start":   s["start_str"],
            "target_end":     s["end_str"],
        }

    except Exception as exc:
        return {
            "ok":           False,
            "status":       "capture_failed",
            "failed_stage": "screenshot",
            "error":        str(exc),
        }


# ══════════════════════════════════════════════════════════════════════════════
#  Endpoint: GET /health
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/health", summary="Servis sağlık kontrolü")
async def health():
    """Servisin çalışıp çalışmadığını ve anlık durumunu döndürür."""
    return {
        "ok":                    _browser is not None and _browser.is_connected(),
        "version":               "2.0.0",
        "browser_mode":          "local-playwright-chromium",
        "browserless_configured": False,
        "active_sessions":       len(_sessions),
        "now_istanbul":          datetime.now(TZ_IST).strftime("%d.%m.%Y %H:%M:%S"),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Endpoint: GET /debug/range-target
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/debug/range-target", summary="Hedef seans aralığını hesapla (tarayıcı açılmaz)")
async def debug_range(
    target_date: Optional[str] = Query(
        None, description="YYYY-MM-DD formatında tarih (boşsa bugün)"
    ),
):
    """
    TradingView açmadan hedef tarih/saat hesabını test eder.
    Örnek: /debug/range-target?target_date=2026-06-02
    """
    try:
        start, end = calc_range(target_date)
        return {
            "ok":           True,
            "target_date":  target_date or datetime.now(TZ_IST).strftime("%Y-%m-%d"),
            "target_start": start.strftime("%d.%m.%Y %H:%M"),
            "target_end":   end.strftime("%d.%m.%Y %H:%M"),
            "from_unix":    int(start.timestamp()),
            "to_unix":      int(end.timestamp()),
        }
    except ValueError:
        raise HTTPException(400, detail="Tarih formatı hatalı. YYYY-MM-DD kullanın.")


# ══════════════════════════════════════════════════════════════════════════════
#  Endpoint: GET /prepare-chart
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/prepare-chart", summary="Grafiği hazırla, session_id al")
async def prepare_chart(
    request:     Request,
    symbol:      str           = Query(...,       description="BIST hisse kodu (THYAO, ASELS …)"),
    target_date: Optional[str] = Query(None,      description="YYYY-MM-DD — boşsa güncel grafik"),
    interval:    str           = Query("5m",      description="Sabit 5m — değiştirilemez"),
    view:        str           = Query("session", description="session=tüm seans"),
):
    """
    TradingView'de BIST:SYMBOL grafiğini hazırlar.
    - 5dk mum zorunludur (interval=5)
    - Seans saatleri: 09:55–18:10 Istanbul
    - Başarılıysa session_id döner → /capture-chart'a verilir
    """
    result = await _do_prepare(symbol, target_date)
    status = 200 if result.get("ok") else 500
    return JSONResponse(result, status_code=status)


# ══════════════════════════════════════════════════════════════════════════════
#  Endpoint: GET /capture-chart
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/capture-chart", summary="Hazırlanmış grafiğin screenshot'ını al")
async def capture_chart(
    request:    Request,
    session_id: str = Query(..., description="/prepare-chart'tan dönen session_id"),
):
    """
    Hazırlanmış sayfanın ekran görüntüsünü alır.
    screenshot_url döner — bu URL'yi tarayıcıda açabilir veya GPT'ye verebilirsiniz.
    """
    result = await _do_capture(session_id, get_base(request))
    fs     = result.get("failed_stage", "")
    status = 200 if result.get("ok") else (404 if "not_found" in fs else 500)
    return JSONResponse(result, status_code=status)


# ══════════════════════════════════════════════════════════════════════════════
#  Endpoint: GET /chart-agent  (tek çağrı — debug / hızlı test)
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/chart-agent", summary="Tek çağrıda prepare + capture (debug)")
async def chart_agent(
    request:     Request,
    symbol:      str           = Query(...,  description="BIST hisse kodu"),
    target_date: Optional[str] = Query(None, description="YYYY-MM-DD — boşsa güncel"),
):
    """
    Tek HTTP çağrısında prepare + capture yapar.
    Üretimde Custom GPT iki aşamalı akışı (prepareChart → captureChart) kullanır;
    bu endpoint yalnızca debug ve hızlı test içindir.
    """
    p = await _do_prepare(symbol, target_date)
    if not p.get("ok"):
        return JSONResponse(p, status_code=500)

    c = await _do_capture(p["session_id"], get_base(request))
    return JSONResponse(c, status_code=200 if c.get("ok") else 500)
