#!/usr/bin/env python3
"""
Cloudflare Turnstile token 获取模块

新版 NewAPI（如 GoRouter）的签到接口要求携带 Turnstile token：
服务端会向 Cloudflare siteverify 校验 token（一次性、约 5 分钟有效、绑定提交 IP）。
本模块用无头浏览器在目标站点页面内显式渲染 widget并等待回调拿 token，
随后由 HTTP 客户端以同机同 IP 立即消费。

说明：
- 在数据中心 IP（CI runner/代理出口）上 Turnstile 常进入交互模式，
  等待期间会主动点击 widget 复选框尝试通过。
- sitekey 优先从站点 /api/status 读取，也可由 provider 配置
  turnstile_site_key 直接指定（可绕过被 Cloudflare 拦截的探测请求）。
"""

import asyncio
import os
import time

import httpx
from cloakbrowser import launch_async

from utils.debug import is_debug_enabled
from utils.proxy import get_playwright_proxy

TURNSTILE_SCRIPT_URL = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'
SOLVE_TIMEOUT_SECONDS = 90
CLICK_DELAY_SECONDS = 12
MAX_CLICKS = 4

# 浏览器特征请求头：裸 UA 会提高被 Cloudflare 拦截的概率
BROWSER_LIKE_HEADERS = {
	'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
	'Accept': 'application/json, text/plain, */*',
	'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
	'Accept-Encoding': 'gzip, deflate, br, zstd',
	'Referer': 'https://www.google.com/',
	'Sec-Fetch-Dest': 'empty',
	'Sec-Fetch-Mode': 'cors',
	'Sec-Fetch-Site': 'same-origin',
}

# 注入并渲染 widget：callback 把 token 写到 window.__TS_TOKEN__，
# error/expired 写到 window.__TS_ERROR__，随后由 Python 侧轮询
RENDER_TURNSTILE_JS = (
	"""
async (sitekey) => {
	if (window.__TS_TOKEN__ !== undefined) return 'already-rendered';
	window.__TS_TOKEN__ = undefined;
	window.__TS_ERROR__ = undefined;
	const loadScript = () => new Promise((resolve, reject) => {
		if (window.turnstile) return resolve();
		const script = document.createElement('script');
		script.src = '%s';
		script.async = true;
		script.onload = () => resolve();
		script.onerror = () => reject(new Error('failed to load turnstile api.js'));
		document.head.appendChild(script);
	});
	await loadScript();
	const container = document.createElement('div');
	container.id = '__ts_container__';
	container.style.cssText = 'position:fixed;bottom:0;right:0;width:300px;height:65px;z-index:99999;';
	document.body.appendChild(container);
	window.turnstile.render(container, {
		sitekey,
		callback: (token) => { window.__TS_TOKEN__ = token; },
		'error-callback': (code) => { window.__TS_ERROR__ = 'error-callback: ' + code; },
		'expired-callback': () => { window.__TS_ERROR__ = 'token expired before capture'; },
	});
	return 'rendered';
}
"""
	% TURNSTILE_SCRIPT_URL
)

# 获取 widget iframe 在页面中的位置（交互模式下需要点击复选框）
GET_WIDGET_RECT_JS = """
() => {
	const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
	if (!iframe) return null;
	const rect = iframe.getBoundingClientRect();
	return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
}
"""


def is_headless_enabled() -> bool:
	return os.getenv('CHECKIN_HEADLESS', 'true') != 'false'


def _debug_screenshot(page, label: str) -> None:
	"""调试模式下保存截图，便于诊断 Turnstile 交互状态"""
	if not is_debug_enabled():
		return
	try:
		os.makedirs('checkin_screenshots', exist_ok=True)
		path = f'checkin_screenshots/turnstile-{label}.png'
		page.screenshot(path=path)
		print(f'[DEBUG] Turnstile screenshot saved to {path}')
	except Exception as e:
		print(f'[WARN] Failed to save turnstile screenshot: {e}')


async def _request_status(domain: str, proxy_url: str | None) -> str | None:
	"""请求 /api/status 并返回 turnstile_site_key（失败返回 None）"""
	client_kwargs: dict = {'http2': True, 'timeout': 30.0}
	if proxy_url:
		client_kwargs['proxy'] = proxy_url
	async with httpx.AsyncClient(**client_kwargs) as client:
		response = await client.get(f'{domain}/api/status', headers=BROWSER_LIKE_HEADERS)
		data = response.json().get('data', {})
		return data.get('turnstile_site_key') or None


async def fetch_turnstile_site_key(domain: str, use_proxy: bool = False) -> str | None:
	"""从站点的 /api/status 读取 Turnstile site key；优先走代理，失败回退直连"""
	from utils.proxy import get_proxy_server

	proxy_url = get_proxy_server(use_proxy=use_proxy)
	attempts = [('proxy', proxy_url), ('direct', None)] if proxy_url else [('direct', None)]
	for label, proxy_url in attempts:
		try:
			site_key = await _request_status(domain, proxy_url)
			if site_key:
				print(f'[INFO] Turnstile site key fetched via {label}')
				return site_key
			print(f'[WARN] Turnstile site key missing in /api/status via {label}')
		except Exception as e:
			print(f'[WARN] Failed to fetch turnstile site key from {domain} via {label}: {str(e)[:80]}')
	return None


async def _try_click_checkbox(page) -> bool:
	"""尝试点击 Turnstile widget 的复选框（交互模式），成功返回 True"""
	try:
		rect = await page.evaluate(GET_WIDGET_RECT_JS)
		if not rect or not rect.get('width'):
			return False
		x = rect['x'] + min(30, rect['width'] / 2)
		y = rect['y'] + rect['height'] / 2
		await page.mouse.click(x, y)
		print('[INFO] Clicked Turnstile checkbox for interactive challenge')
		return True
	except Exception as e:
		print(f'[WARN] Turnstile checkbox click failed: {str(e)[:80]}')
		return False


async def solve_turnstile_token(
	domain: str,
	site_key: str,
	*,
	use_proxy: bool = False,
	label: str = 'site',
) -> str | None:
	"""用无头浏览器在站点页面内解决 Turnstile，返回一次性 token（失败返回 None）"""
	print('[PROCESSING] Launching browser to solve Turnstile challenge...')
	try:
		launch_kwargs: dict = {'headless': is_headless_enabled()}
		proxy = get_playwright_proxy(use_proxy=use_proxy)
		if proxy:
			launch_kwargs['proxy'] = proxy
		browser = await launch_async(**launch_kwargs)
	except Exception as e:
		print(f'[FAILED] Browser launch failed while solving Turnstile: {e}')
		return None

	try:
		page = await browser.new_page()
		await page.goto(domain, wait_until='domcontentloaded')
		await page.evaluate(RENDER_TURNSTILE_JS, site_key)

		deadline = time.monotonic() + SOLVE_TIMEOUT_SECONDS
		next_click_at = time.monotonic() + CLICK_DELAY_SECONDS
		clicks = 0
		token: str | None = None
		while time.monotonic() < deadline:
			state = await page.evaluate(
				'() => ({ token: window.__TS_TOKEN__ ?? null, error: window.__TS_ERROR__ ?? null })'
			)
			token = state.get('token')
			if token:
				print(f'[SUCCESS] Turnstile token captured (clicks={clicks})')
				return token
			if state.get('error'):
				print(f'[FAILED] Turnstile reported error: {state["error"]}')
				_debug_screenshot(page, f'{label}-error')
				return None
			if time.monotonic() >= next_click_at and clicks < MAX_CLICKS:
				if await _try_click_checkbox(page):
					clicks += 1
				next_click_at = time.monotonic() + CLICK_DELAY_SECONDS
			await asyncio.sleep(2)

		print(f'[FAILED] Turnstile solve timeout after {SOLVE_TIMEOUT_SECONDS}s (clicks={clicks})')
		_debug_screenshot(page, f'{label}-timeout')
		return None
	except Exception as e:
		print(f'[FAILED] Error occurred while solving Turnstile: {str(e)[:100]}')
		_debug_screenshot(page, f'{label}-exception')
		return None
	finally:
		await browser.close()
