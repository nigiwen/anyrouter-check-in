#!/usr/bin/env python3
"""
Cloudflare Turnstile token 获取模块

新版 NewAPI（如 GoRouter）的签到接口要求携带 Turnstile token：
服务端会向 Cloudflare siteverify 校验 token（一次性、约 5 分钟有效、绑定提交 IP）。
本模块用无头浏览器在目标站点页面内显式渲染 widget 并等待回调拿 token，
随后由 HTTP 客户端以同机同 IP 立即消费。
"""

import asyncio
import os

from cloakbrowser import launch_async

from utils.proxy import get_playwright_proxy

TURNSTILE_SCRIPT_URL = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'
SOLVE_TIMEOUT_SECONDS = 90

# 在目标站点页面内显式渲染 Turnstile widget，等待 callback 返回一次性 token
SOLVE_TURNSTILE_JS = (
	"""
async ([sitekey, timeoutSeconds]) => {
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
	return await new Promise((resolve, reject) => {
		const timer = setTimeout(() => reject(new Error('turnstile solve timeout')), timeoutSeconds * 1000);
		const container = document.createElement('div');
		container.style.cssText = 'position:fixed;bottom:0;right:0;width:300px;height:65px;';
		document.body.appendChild(container);
		try {
			window.turnstile.render(container, {
				sitekey,
				callback: (token) => { clearTimeout(timer); resolve(token); },
				'error-callback': (code) => { clearTimeout(timer); reject(new Error('turnstile error-callback: ' + code)); },
				'expired-callback': () => { clearTimeout(timer); reject(new Error('turnstile token expired before capture')); },
			});
		} catch (error) {
			clearTimeout(timer);
			reject(error);
		}
	});
}
"""
	% TURNSTILE_SCRIPT_URL
)


def is_headless_enabled() -> bool:
	return os.getenv('CHECKIN_HEADLESS', 'true') != 'false'


async def fetch_turnstile_site_key(domain: str, use_proxy: bool = False) -> str | None:
	"""从站点的 /api/status 读取 Turnstile site key"""
	import httpx

	try:
		async with httpx.AsyncClient(http2=True, timeout=30.0) as client:
			response = await client.get(f'{domain}/api/status', headers={'User-Agent': 'Mozilla/5.0'})
			data = response.json().get('data', {})
			site_key = data.get('turnstile_site_key')
			return site_key or None
	except Exception as e:
		print(f'[FAILED] Failed to fetch turnstile site key from {domain}: {str(e)[:80]}')
		return None


async def solve_turnstile_token(
	domain: str,
	site_key: str,
	*,
	use_proxy: bool = False,
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
		token: str | None = await asyncio.wait_for(
			page.evaluate(SOLVE_TURNSTILE_JS, [site_key, SOLVE_TIMEOUT_SECONDS]),
			timeout=SOLVE_TIMEOUT_SECONDS + 15,
		)
		if token:
			print('[SUCCESS] Turnstile token captured')
			return token
		print('[FAILED] Turnstile returned an empty token')
		return None
	except Exception as e:
		print(f'[FAILED] Error occurred while solving Turnstile: {str(e)[:100]}')
		return None
	finally:
		await browser.close()
