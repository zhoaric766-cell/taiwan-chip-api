"""
台股籌碼 API 中介服務 v2.0
- 修正日期邏輯:預設抓「上一個交易日」,不抓今日(避免盤中沒資料)
- 重寫 TWSE JSON 解析,適配新版回傳格式
- 重寫 TAIFEX HTML 解析,使用更穩健的方式
- 加入大量 debug log,方便從 Render Logs 找錯
"""

from flask import Flask, jsonify, request
import requests
from datetime import datetime, timedelta
import re
import logging

app = Flask(__name__)

# 強制 log 顯示在 Render console
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/html, */*',
    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
    'Cache-Control': 'no-cache'
}


# =============================================================
# 共用工具
# =============================================================

def get_target_date(date_str=None, default_offset_days=1):
    """
    取得目標交易日。
    - 若有指定 date_str,直接用
    - 若沒指定,預設抓「昨天」(default_offset_days=1),避開今日尚未公告的狀況
    - 自動跳過週末
    """
    if date_str:
        try:
            return datetime.strptime(date_str, '%Y-%m-%d')
        except Exception:
            pass

    # 預設往前推 default_offset_days 天
    d = datetime.now() - timedelta(days=default_offset_days)
    # 跳過週末
    for _ in range(7):
        if d.weekday() < 5:  # 0=週一, 4=週五
            return d
        d -= timedelta(days=1)
    return datetime.now() - timedelta(days=1)


def to_num(v):
    """把字串轉數字,失敗回 0"""
    if v is None or v == '' or v == '-' or v == '--':
        return 0
    s = str(v).replace(',', '').replace(' ', '').strip()
    # 處理括號(代表負數)
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    try:
        return float(s)
    except Exception:
        return 0


def parse_query_date():
    """從 request 取得 date 參數,沒有就用昨天"""
    date_str = request.args.get('date')
    return get_target_date(date_str)


def fetch_json(url, name='unknown'):
    """打 TWSE JSON API,回傳 dict"""
    try:
        logger.info(f'[{name}] fetching: {url}')
        res = requests.get(url, headers=HEADERS, timeout=20)
        logger.info(f'[{name}] status: {res.status_code}')
        if res.status_code != 200:
            logger.warning(f'[{name}] non-200 response, body: {res.text[:300]}')
            return {}
        data = res.json()
        # 印出有什麼 keys 方便 debug
        logger.info(f'[{name}] keys: {list(data.keys())[:10]}')
        return data
    except Exception as e:
        logger.error(f'[{name}] fetch_json error: {e}')
        return {}


def extract_rows(data):
    """從 TWSE JSON 中萃取所有資料列(支援新舊格式)"""
    rows = []
    # 新格式:tables[].data
    tables = data.get('tables', [])
    if isinstance(tables, list):
        for t in tables:
            if isinstance(t, dict):
                rows.extend(t.get('data', []) or [])
    # 舊格式:data 直接是 list
    if not rows:
        rows = data.get('data', []) or []
    return rows


# =============================================================
# 首頁
# =============================================================

@app.route('/')
def index():
    return jsonify({
        'service': '台股籌碼 API 中介服務',
        'version': 'v2.0',
        'note': '預設抓「上一個交易日」,可加 ?date=YYYY-MM-DD 指定',
        'endpoints': [
            '/futures',
            '/options',
            '/pcr',
            '/taiex',
            '/margin',
            '/institutional',
            '/all',
            '/debug/futures   (顯示 TAIFEX 原始回應前 1000 字)',
            '/debug/options   (顯示 TAIFEX 原始回應前 1000 字)',
            '/debug/pcr       (顯示 TAIFEX 原始回應前 1000 字)'
        ]
    })


# =============================================================
# /taiex — 加權指數
# =============================================================

@app.route('/taiex')
def taiex():
    target = parse_query_date()
    date_compact = target.strftime('%Y%m%d')
    date_fmt = target.strftime('%Y/%m/%d')
    roc_date = f'{target.year - 1911}/{target.month:02d}/{target.day:02d}'

    result = {
        'index': 0, 'change': 0, 'changePct': 0,
        'high': 0, 'low': 0, 'range': 0,
        'turnover_yi': 0, 'date': date_fmt
    }

    # MI_INDEX = 大盤指數每日行情(單日查詢)
    url = f'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={date_compact}&response=json'
    data = fetch_json(url, name='taiex_mi_index')
    rows = extract_rows(data)
    logger.info(f'[taiex] MI_INDEX got {len(rows)} rows')

    for row in rows:
        if not row or len(row) < 2:
            continue
        label = str(row[0]).strip()
        if '發行量加權' in label or '加權股價指數' in label:
            logger.info(f'[taiex] found index row: {row}')
            try:
                idx = to_num(row[1])
                change = 0
                sign = 1
                for i in range(2, len(row)):
                    v = str(row[i]).strip()
                    if v in ('-', '▼'):
                        sign = -1
                        continue
                    if v in ('+', '▲', ''):
                        continue
                    change = sign * abs(to_num(v))
                    break
                prev = idx - change
                pct = round(change / prev * 100, 2) if prev else 0
                result.update({'index': round(idx, 2), 'change': round(change, 2), 'changePct': pct})
            except Exception as e:
                logger.error(f'[taiex] parse index row error: {e}')
            break

    # FMTQIK = 月資料,補成交金額 + 備援指數
    url2 = f'https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={date_compact}&response=json'
    data2 = fetch_json(url2, name='taiex_fmtqik')
    rows2 = extract_rows(data2)
    roc_date_clean = roc_date.replace(' ', '')
    logger.info(f'[taiex] FMTQIK got {len(rows2)} rows, looking for {roc_date_clean}')
    for row in rows2:
        if not row or len(row) < 3:
            continue
        if str(row[0]).replace(' ', '').strip() == roc_date_clean:
            result['turnover_yi'] = round(to_num(row[2]) / 100000000, 2)
            if result['index'] == 0 and len(row) > 5:
                idx = to_num(row[4]); change = to_num(row[5])
                prev = idx - change
                result.update({'index': round(idx,2), 'change': round(change,2),
                               'changePct': round(change/prev*100,2) if prev else 0})
            break

    # MI_5MINS_HIST = 大盤指數高低點
    try:
        url2 = f'https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date={date_compact}&response=json'
        data2 = fetch_json(url2, name='taiex_hist')
        rows2 = extract_rows(data2)
        for row in rows2:
            if not row or len(row) < 4:
                continue
            row_date = str(row[0]).replace(' ', '').strip()
            if row_date == roc_date.replace(' ', ''):
                # row 結構:[日期, 開盤, 最高, 最低, 收盤]
                high = to_num(row[2])
                low = to_num(row[3])
                result.update({'high': round(high, 2), 'low': round(low, 2),
                               'range': round(high - low, 2)})
                break
    except Exception as e:
        logger.error(f'[taiex] hist error: {e}')

    return jsonify(result)


# =============================================================
# /institutional — 三大法人現貨
# =============================================================

@app.route('/institutional')
def institutional():
    target = parse_query_date()
    date_compact = target.strftime('%Y%m%d')
    date_fmt = target.strftime('%Y/%m/%d')

    result = {
        'foreign': 0, 'prop_self': 0,
        'prop_hedge': 0, 'trust': 0,
        'date': date_fmt
    }

    url = f'https://www.twse.com.tw/rwd/zh/fund/BFI82U?dayDate={date_compact}&type=day&response=json'
    data = fetch_json(url, name='institutional')
    rows = extract_rows(data)
    logger.info(f'[institutional] got {len(rows)} rows')

    for row in rows:
        if not row or len(row) < 4:
            continue
        role = str(row[0]).strip()
        # 買賣差額通常在最後一欄;不同欄數版本要適應
        net_raw = row[-1] if len(row) >= 4 else 0
        net = round(to_num(net_raw) / 100000000, 2)

        logger.info(f'[institutional] role={role}, net={net}')

        if '自營商' in role and '避險' in role:
            result['prop_hedge'] = net
        elif '自營商' in role and ('自行' in role or '自營' in role):
            result['prop_self'] = net
        elif '投信' in role:
            result['trust'] = net
        elif '外資' in role and '不含' not in role:
            # 「外資及陸資」或「外資」皆可
            result['foreign'] = net

    return jsonify(result)


# =============================================================
# /margin — 融資融券
# =============================================================

@app.route('/margin')
def margin():
    target = parse_query_date()
    date_compact = target.strftime('%Y%m%d')
    date_fmt = target.strftime('%Y/%m/%d')

    result = {'margin_balance': 0, 'short_units': 0, 'date': date_fmt}

    url = f'https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date_compact}&selectType=MS&response=json'
    data = fetch_json(url, name='margin')

    # MI_MARGN 有多張表,我們要的是「整體市場」融資融券餘額
    tables = data.get('tables', [])
    logger.info(f'[margin] got {len(tables)} tables')

    for ti, t in enumerate(tables):
        title = t.get('title', '')
        rows = t.get('data', []) or []
        logger.info(f'[margin] table {ti}: title="{title}", rows={len(rows)}')
        for row in rows:
            if not row or len(row) < 6:
                continue
            cat = str(row[0]).strip()
            logger.info(f'[margin] row: {row}')
            # 融資餘額(在「融資」那一列的「今日餘額」)
            if cat == '融資':
                # row 結構通常:[類別, 前日餘額, 買進, 賣出, 現償, 今日餘額, ...]
                result['margin_balance'] = int(to_num(row[5]))
            elif cat == '融券':
                result['short_units'] = int(to_num(row[5]))

    return jsonify(result)


# =============================================================
# /futures — 期貨三大法人(TAIFEX)
# =============================================================

def fetch_taifex_html(url, params, name='taifex'):
    """打 TAIFEX,自動處理 Big5 編碼"""
    try:
        logger.info(f'[{name}] fetching: {url} params={params}')
        res = requests.get(url, params=params, headers=HEADERS, timeout=20)
        logger.info(f'[{name}] status: {res.status_code}, length: {len(res.content)}')
        # TAIFEX 用 Big5,但有時候也會用 UTF-8;試兩種
        try:
            res.encoding = 'big5'
            text = res.text
            if '外資' not in text and '自營' not in text:
                res.encoding = 'utf-8'
                text = res.text
        except Exception:
            text = res.text
        return text
    except Exception as e:
        logger.error(f'[{name}] fetch error: {e}')
        return ''


def parse_taifex_table(html):
    """解析 TAIFEX 表格,回傳 list of (cells)"""
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE)
    parsed = []
    for r in rows:
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r, re.DOTALL | re.IGNORECASE)
        cells = [re.sub(r'<[^>]+>', '', c).replace('&nbsp;', ' ').strip() for c in cells]
        if cells:
            parsed.append(cells)
    return parsed


@app.route('/futures')
def futures():
    target = parse_query_date()
    date_fmt = target.strftime('%Y/%m/%d')

    result = {
        'foreign_TXF_oi': 0, 'foreign_MXF_oi': 0,
        'prop_TXF_oi': 0, 'prop_MXF_oi': 0,
        'date': date_fmt
    }

    products = [
        ('TXF', 'foreign_TXF_oi', 'prop_TXF_oi'),
        ('MXF', 'foreign_MXF_oi', 'prop_MXF_oi'),
    ]

    for prod_code, fkey, pkey in products:
        url = 'https://www.taifex.com.tw/cht/3/futContractsDate'
        params = {
            'queryStartDate': target.strftime('%Y/%m/%d'),
            'queryEndDate': target.strftime('%Y/%m/%d'),
            'commodityId': prod_code
        }
        html = fetch_taifex_html(url, params, name=f'futures_{prod_code}')
        if not html:
            continue

        rows = parse_taifex_table(html)
        logger.info(f'[futures_{prod_code}] parsed {len(rows)} rows')

        for cells in rows:
            row_text = ' | '.join(cells)
            # 確認是不是身份別列
            if '自營商' in row_text:
                logger.info(f'[futures_{prod_code}] prop row: {cells}')
                # 找未平倉口數淨額(通常在較後面的欄位,先抓所有純數字 cell)
                nums = [c for c in cells if re.match(r'^-?[\d,]+$', c)]
                if len(nums) >= 11:
                    # 期貨身份別表結構:
                    # 多方口數, 多方契約金額, 空方口數, 空方契約金額, 多空淨口數, 多空淨契約金額,
                    # 多方未平倉口數, 多方未平倉契約金額, 空方未平倉口數, 空方未平倉契約金額,
                    # 未平倉口數淨額, 未平倉契約金額淨額
                    net_oi = int(to_num(nums[10]))  # 第 11 個 = 未平倉口數淨額
                    result[pkey] = net_oi
                    logger.info(f'[futures_{prod_code}] prop net OI: {net_oi}')
            elif '外資' in row_text:
                logger.info(f'[futures_{prod_code}] foreign row: {cells}')
                nums = [c for c in cells if re.match(r'^-?[\d,]+$', c)]
                if len(nums) >= 11:
                    net_oi = int(to_num(nums[10]))
                    result[fkey] = net_oi
                    logger.info(f'[futures_{prod_code}] foreign net OI: {net_oi}')

    return jsonify(result)


# =============================================================
# /options — 選擇權三大法人(TAIFEX)
# =============================================================

@app.route('/options')
def options():
    target = parse_query_date()
    date_fmt = target.strftime('%Y/%m/%d')

    result = {
        'foreign_BC_oi': 0, 'foreign_BC_amt': 0,
        'foreign_SC_oi': 0, 'foreign_SC_amt': 0,
        'foreign_BP_oi': 0, 'foreign_BP_amt': 0,
        'foreign_SP_oi': 0, 'foreign_SP_amt': 0,
        'prop_BC_oi': 0, 'prop_BC_amt': 0,
        'prop_SC_oi': 0, 'prop_SC_amt': 0,
        'prop_BP_oi': 0, 'prop_BP_amt': 0,
        'prop_SP_oi': 0, 'prop_SP_amt': 0,
        'date': date_fmt
    }

    url = 'https://www.taifex.com.tw/cht/3/callsAndPutsDate'
    params = {
        'queryStartDate': target.strftime('%Y/%m/%d'),
        'queryEndDate': target.strftime('%Y/%m/%d'),
        'commodityId': 'TXO'
    }
    html = fetch_taifex_html(url, params, name='options')
    if not html:
        return jsonify(result)

    rows = parse_taifex_table(html)
    logger.info(f'[options] parsed {len(rows)} rows')

    current_cp = None  # 'call' or 'put'

    for cells in rows:
        row_text = ' | '.join(cells)

        # 判斷買權/賣權
        if any('買權' in c for c in cells):
            current_cp = 'call'
        elif any('賣權' in c for c in cells):
            current_cp = 'put'

        if not current_cp:
            continue

        # 判斷身份
        is_foreign = any('外資' in c and '陸資' not in c for c in cells)
        is_prop = any('自營商' in c for c in cells)

        if not (is_foreign or is_prop):
            continue

        nums = [c for c in cells if re.match(r'^-?[\d,]+$', c)]
        logger.info(f'[options] {current_cp} {"foreign" if is_foreign else "prop"}: nums={nums}')

        # 選擇權身份別表結構(每身份別會有 12 個數字):
        # 買方口數, 買方金額, 賣方口數, 賣方金額, 買賣差口數, 買賣差金額,
        # 買方未平倉口數, 買方未平倉金額, 賣方未平倉口數, 賣方未平倉金額,
        # 未平倉淨口數, 未平倉淨金額
        if len(nums) < 10:
            continue

        # 取「買方未平倉口數/金額、賣方未平倉口數/金額」(第 7~10 個)
        b_oi = int(to_num(nums[6]))
        b_amt = int(to_num(nums[7]))
        s_oi = int(to_num(nums[8]))
        s_amt = int(to_num(nums[9]))

        if is_foreign:
            if current_cp == 'call':
                result['foreign_BC_oi'] = b_oi; result['foreign_BC_amt'] = b_amt
                result['foreign_SC_oi'] = s_oi; result['foreign_SC_amt'] = s_amt
            else:
                result['foreign_BP_oi'] = b_oi; result['foreign_BP_amt'] = b_amt
                result['foreign_SP_oi'] = s_oi; result['foreign_SP_amt'] = s_amt
        elif is_prop:
            if current_cp == 'call':
                result['prop_BC_oi'] = b_oi; result['prop_BC_amt'] = b_amt
                result['prop_SC_oi'] = s_oi; result['prop_SC_amt'] = s_amt
            else:
                result['prop_BP_oi'] = b_oi; result['prop_BP_amt'] = b_amt
                result['prop_SP_oi'] = s_oi; result['prop_SP_amt'] = s_amt

    return jsonify(result)


# =============================================================
# /pcr — Put/Call Ratio(TAIFEX)
# =============================================================

@app.route('/pcr')
def pcr():
    target = parse_query_date()
    date_fmt = target.strftime('%Y/%m/%d')

    result = {'pcr_oi': 0, 'pcr_volume': 0, 'date': date_fmt}

    url = 'https://www.taifex.com.tw/cht/3/pcRatio'
    # 加 queryStartDate / queryEndDate 取得指定日期範圍
    params = {
        'queryStartDate': target.strftime('%Y/%m/%d'),
        'queryEndDate': target.strftime('%Y/%m/%d')
    }
    html = fetch_taifex_html(url, params, name='pcr')
    if not html:
        return jsonify(result)

    rows = parse_taifex_table(html)
    logger.info(f'[pcr] parsed {len(rows)} rows')

    target_str = target.strftime('%Y/%m/%d')

    for cells in rows:
        if len(cells) < 5:
            continue
        # 第一格如果是日期
        first = cells[0]
        if re.match(r'\d{4}/\d{2}/\d{2}', first):
            logger.info(f'[pcr] date row: {cells}')
            # 結構通常:[日期, 賣權交易量, 買權交易量, P/C 比, 賣權OI, 買權OI, P/C OI 比]
            # 取最後一欄 = OI 比;P/C 量比通常在第 4 欄
            try:
                if first == target_str or result['pcr_oi'] == 0:
                    # 如果欄位是百分比格式(例如 89.42),除以 100
                    vol_raw = to_num(cells[3]) if len(cells) > 3 else 0
                    oi_raw = to_num(cells[6]) if len(cells) > 6 else to_num(cells[-1])
                    # 自動偵測:如果數字 > 5 就當百分比
                    pcr_vol = round(vol_raw / 100, 4) if vol_raw > 5 else round(vol_raw, 4)
                    pcr_oi = round(oi_raw / 100, 4) if oi_raw > 5 else round(oi_raw, 4)
                    result['pcr_volume'] = pcr_vol
                    result['pcr_oi'] = pcr_oi
                    if first == target_str:
                        break
            except Exception as e:
                logger.error(f'[pcr] parse error: {e}')

    return jsonify(result)


# =============================================================
# Debug endpoints — 看 TAIFEX 真的回傳了什麼
# =============================================================

@app.route('/debug/futures')
def debug_futures():
    target = parse_query_date()
    url = 'https://www.taifex.com.tw/cht/3/futContractsDate'
    params = {
        'queryStartDate': target.strftime('%Y/%m/%d'),
        'queryEndDate': target.strftime('%Y/%m/%d'),
        'commodityId': 'TXF'
    }
    html = fetch_taifex_html(url, params, name='debug_futures')
    rows = parse_taifex_table(html)
    return jsonify({
        'date': target.strftime('%Y/%m/%d'),
        'html_len': len(html),
        'rows_count': len(rows),
        'first_5_rows': rows[:5],
        'rows_with_foreign': [r for r in rows if any('外資' in c for c in r)][:3],
        'rows_with_prop': [r for r in rows if any('自營商' in c for c in r)][:3],
    })


@app.route('/debug/options')
def debug_options():
    target = parse_query_date()
    url = 'https://www.taifex.com.tw/cht/3/callsAndPutsDate'
    params = {
        'queryStartDate': target.strftime('%Y/%m/%d'),
        'queryEndDate': target.strftime('%Y/%m/%d'),
        'commodityId': 'TXO'
    }
    html = fetch_taifex_html(url, params, name='debug_options')
    rows = parse_taifex_table(html)
    return jsonify({
        'date': target.strftime('%Y/%m/%d'),
        'html_len': len(html),
        'rows_count': len(rows),
        'first_8_rows': rows[:8],
    })


@app.route('/debug/pcr')
def debug_pcr():
    target = parse_query_date()
    url = 'https://www.taifex.com.tw/cht/3/pcRatio'
    params = {
        'queryStartDate': target.strftime('%Y/%m/%d'),
        'queryEndDate': target.strftime('%Y/%m/%d')
    }
    html = fetch_taifex_html(url, params, name='debug_pcr')
    rows = parse_taifex_table(html)
    return jsonify({
        'date': target.strftime('%Y/%m/%d'),
        'html_len': len(html),
        'rows_count': len(rows),
        'first_8_rows': rows[:8],
    })


@app.route('/debug/twse_taiex')
def debug_twse_taiex():
    target = parse_query_date()
    url = f'https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={target.strftime("%Y%m%d")}&response=json'
    data = fetch_json(url, name='debug_twse_taiex')
    rows = extract_rows(data)
    return jsonify({
        'target_date': target.strftime('%Y/%m/%d'),
        'roc_date_expected': f'{target.year - 1911}/{target.month:02d}/{target.day:02d}',
        'data_keys': list(data.keys())[:20],
        'rows_count': len(rows),
        'last_5_rows': rows[-5:] if rows else []
    })


# =============================================================
# /all — 一次拿所有
# =============================================================

@app.route('/all')
def all_data():
    date_str = request.args.get('date')
    qs = f'?date={date_str}' if date_str else ''

    out = {}
    with app.test_request_context('/' + qs):
        out['taiex'] = taiex().get_json()
    with app.test_request_context('/' + qs):
        out['institutional'] = institutional().get_json()
    with app.test_request_context('/' + qs):
        out['margin'] = margin().get_json()
    with app.test_request_context('/' + qs):
        out['futures'] = futures().get_json()
    with app.test_request_context('/' + qs):
        out['options'] = options().get_json()
    with app.test_request_context('/' + qs):
        out['pcr'] = pcr().get_json()
    return jsonify(out)


if __name__ == '__main__':
    app.run(debug=False, port=10000)
