# -- coding: utf-8 --
import os
import requests
from curl_cffi import requests as browser_requests
import re
import sys
import json
from datetime import datetime

def parse_expiry_date(expiry_str):
    """解析过期日期（格式：YYYY-MM-DD），返回剩余天数"""
    if not expiry_str:
        return None
    try:
        expiry = datetime.strptime(expiry_str.strip(), "%Y-%m-%d")
        delta = expiry - datetime.now()
        return delta.days
    except Exception:
        return None

def send_telegram_message(text):
    bot_token = os.getenv('TG_BOT_TOKEN')
    chat_id = os.getenv('TG_CHAT_ID')
    if not bot_token or not chat_id:
        print("未配置 Telegram 通知，跳过。")
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, json={
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML'
        }, timeout=10)
        if resp.status_code == 200:
            print("Telegram 通知发送成功。")
        else:
            print(f"Telegram 通知失败: {resp.text}")
    except Exception as e:
        print(f"发送 Telegram 异常: {e}")

def response_diagnostics(resp):
    """只输出分类和布尔信息，不记录 Cookie、响应正文或原始响应头。"""
    body = resp.text[:200000].lower()
    challenge = (
        resp.headers.get('cf-mitigated', '').lower() == 'challenge'
        or any(marker in body for marker in (
            '/cdn-cgi/challenge-platform/', 'cf-chl-',
            'just a moment...', 'checking your browser',
        ))
    )
    content_type = resp.headers.get('Content-Type', '').lower()
    response_type = 'json' if 'json' in content_type else (
        'html' if 'html' in content_type else 'other'
    )
    auth_error = False
    if response_type == 'json':
        try:
            data = resp.json()
            if isinstance(data, dict):
                message = str(data.get('message', ''))
                auth_error = bool(re.search(
                    r'未登录|请.*登录|登录.*(过期|失效)|无权限|no permission|unauthorized|not logged',
                    message, re.I,
                ))
        except ValueError:
            pass
    if challenge:
        category = 'cloudflare_challenge'
    elif auth_error or resp.status_code == 401:
        category = 'authentication_rejected'
    elif resp.status_code == 403:
        category = 'forbidden_unknown'
    elif resp.status_code != 200:
        category = 'http_error'
    else:
        category = 'application_response'
    # requests 与 curl_cffi 的 Cookie 容器都支持 keys()。
    cookie_names = set(resp.cookies.keys())
    diagnostics = {
        'http_status': resp.status_code,
        'response_type': response_type,
        'cloudflare_server': resp.headers.get('Server', '').lower() == 'cloudflare',
        'cloudflare_ray_present': bool(resp.headers.get('cf-ray')),
        'challenge_detected': challenge,
        'auth_error_detected': auth_error,
        'category': category,
        'set_cookie_present': 'Set-Cookie' in resp.headers,
        'response_cookie_count': len(cookie_names),
        'clearance_cookie_updated': 'cf_clearance' in cookie_names,
        'known_login_cookie_updated': bool(cookie_names & {'session', 'pjwt'}),
        'redirect_count': len(resp.history),
    }
    print('[诊断] ' + json.dumps(diagnostics, ensure_ascii=False))
    return category


def checkin(cookie, random_mode=False):
    """签到函数，使用正确的 API 方式：POST /api/attendance?random=true/false，body为空"""
    random_param = 'true' if random_mode else 'false'
    url = f"https://www.nodeseek.com/api/attendance?random={random_param}"
    
    headers = {
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh-Hans;q=0.9,en;q=0.8',
        'Content-Type': 'application/json',
        'Cookie': cookie,
        'Origin': 'https://www.nodeseek.com',
        'Referer': 'https://www.nodeseek.com/board',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        # User-Agent 由 Chrome 模拟配置生成，避免与 TLS 指纹不匹配。
    }
    
    try:
        resp = browser_requests.post(
            url, headers=headers, timeout=15, impersonate='chrome136'
        )
    except Exception as e:
        return False, f"请求异常: {e}", 0

    category = response_diagnostics(resp)
    if resp.status_code != 200:
        if resp.status_code == 500:
            if '已签到' in resp.text or '重复' in resp.text:
                return True, "已签到（今日已打卡）", 0
        reason = {
            'cloudflare_challenge': 'Cloudflare 验证拦截',
            'authentication_rejected': '登录认证被拒绝',
            'forbidden_unknown': '访问被拒绝，原因未明',
        }.get(category, '接口异常')
        return False, f"HTTP {resp.status_code}（{reason}）", 0

    try:
        result = resp.json()
    except:
        return False, f"非JSON响应: {resp.text[:100]}", 0

    success = result.get('success', False)
    msg = result.get('message', '')
    state = result.get('state', '')

    if not success and re.search(r'(已完成签到|已签到|重复|already|duplicate)', msg, re.I):
        return True, "已签到（今日已打卡）", 0

    chicken = 0
    if success or state == 'success':
        m = re.search(r'获得(\d+)鸡腿', msg)
        if m:
            chicken = int(m.group(1))
        return True, msg, chicken

    return False, msg, 0

def main():
    cookies_raw = os.getenv('NS_COOKIES')
    if not cookies_raw:
        print("错误: 未设置 NS_COOKIES")
        sys.exit(1)

    random_mode = os.getenv('NS_RANDOM', 'false').strip().lower() == 'true'

    lines = [line.strip() for line in cookies_raw.split('\n') if line.strip()]
    if not lines:
        print("错误: NS_COOKIES 为空")
        sys.exit(1)

    print(f"签到模式: {'试试手气' if random_mode else '固定鸡腿'}")
    print(f"检测到 {len(lines)} 个账号，开始签到...")
    results = []

    # 解析每行，支持格式：用户名|Cookie|到期日期（可选）
    accounts = []
    for line in lines:
        parts = line.split('|')
        if len(parts) >= 3:
            username = parts[0].strip()
            cookie = parts[1].strip()
            expiry_date = parts[2].strip()
        elif len(parts) == 2:
            username = parts[0].strip()
            cookie = parts[1].strip()
            expiry_date = None
        else:
            username = None
            cookie = line
            expiry_date = None
        accounts.append((username, cookie, expiry_date))

    for idx, (username, cookie, expiry_date) in enumerate(accounts, 1):
        display_name = username if username else f"账号 {idx}"

        # 计算该账号的剩余天数
        days_left = parse_expiry_date(expiry_date)
        days_str = f"{days_left} 天" if days_left is not None else "未知"

        success, msg, chicken = checkin(cookie, random_mode)
        status_icon = "✅" if success else "❌"
        if success and chicken == 0:
            numbers = re.findall(r'\d+', msg)
            if numbers:
                chicken = int(numbers[0])

        result_line = f"{display_name}: {status_icon} {msg}\n 获得 {chicken} 鸡腿 \n cookie到期剩余 {days_str} \n https://www.nodeseek.com/"
        results.append(result_line)
        print(result_line)

    final_msg = "<b>📅 NodeSeek 签到汇总</b>\n" + "\n".join(results)
    send_telegram_message(final_msg)

if __name__ == "__main__":
    main()
