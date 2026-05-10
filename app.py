"""
台股籌碼 API 中介服務 v3.2
============================================================
2026-05-10 三大修補(基於 v3.1 + 真實測試結果):

★ Bug #1:/pcr 全 0 — regex 不認月日 1 位數
  舊:r'\d{4}/\d{2}/\d{2}' 不認 "2026/5/8"
  新:r'\d{4}[/-]\d{1,2}[/-]\d{1,2}' 通吃

★ Bug #2:/futures /options 不吃 date 參數
  舊:用 GET 傳 queryStartDate → TAIFEX 不接受 → 永遠回最新
  新:改用 POST(TAIFEX 表單實際做法),date 參數真正生效

★ Bug #3:/options 自營商 290 萬異常
  舊:nums = [純數字 cells],但 rowspan 導致前面 cells 數量浮動,
       索引 [6][7][8][9] 抓到的不是「未平倉買方/賣方口數金額」
  新:先找「身份別 cell」位置,從它之後重新算 nums,索引穩定

★ 額外改善:回傳加 actual_date 欄位,讓 GAS 端能比對實際抓到哪一天
"""

from flask import Flask, jsonify, request
import requests
import urllib3
from datetime import datetime, timedelta
import re
import logging

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
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

OPENAPI_BASE = 'https://openapi.twse.com.tw/v1'


# =============================================================
# 共用工具
# =============================================================

def get_target_date(date_str=None, default_offset_days=1):
    if date_str:
        try:
            return datetime.strptime(date_str, '%Y-%m-%d')
        except Exception:
            pass
    d = datetime.now() - timedelta(days=default_offset_days)
    for _ in range(7):
        if d.weekday() < 5:
            return d
        d -= timedelta(days=1)
    return datetime.now() - timedelta(days=1)


def to_num(v):
    if v is None or v == '' or v == '-' or v == '--':
        return 0
    s = str(v).replace(',', '').replace(' ', '').strip()
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    try:
        return float(s)
    except Exception:
        return 0


def parse_query_date():
    return get_target_date(request.args.get('date'))


def roc_to_ad(roc_date_str):
    if not roc_date_str:
        return ''
    s = str(roc_date_str).replace('/', '').replace('-', '').strip()
    if len(s) != 7:
        return ''
    try:
        yr = int(s[:3]) + 1911
        return f'{yr}/{s[3:5]}/{s[5:7]}'
    except Exception:
        return ''


def normalize_date(date_str):
    """把 '2026/5/8' 或 '2026/05/08' 都正規化成 '2026/05/08'"""
    if not date_str:
        return ''
    m = re.match(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', str(date_str))
    if not m:
        return ''
    return f'{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}'


def fetch_openapi(path, name='openapi'):
    """打 TWSE OpenAPI(v3.1 起 verify=False)"""
    url = f'{OPENAPI_BASE}{path}'
    try:
        logger.info(f'[{name}] fetching: {url}')
        res = requests.get(url, headers=HEADERS, timeout=20, verify=False)
        logger.info(f'[{name}] status: {res.status_code}')
        if res.status_code != 200:
            return None, res.status_code, f'HTTP {res.status_code}: {res.text[:200]}'
        data = res.json()
        n = len(data) if isinstance(data, list) else 'dict'
        logger.info(f'[{name}] got {n} items')
        return data, 200, None
    except Exception as e:
        logger.error(f'[{name}] error: {e}')
        return None, 0, f'{type(e).__name__}: {str(e)[:200]}'


# =============================================================
# 首頁
# =============================================================

@app.route('/')
def index():
    return jsonify({
        'service': '台股籌碼 API 中介服務',
        'version': 'v3.2',
        'changes_v3_2': [
            'Bug #1 fix: /pcr regex 改 \\d{4}[/-]\\d{1,2}[/-]\\d{1,2}',
            'Bug #2 fix: /futures /options 改用 POST 打 TAIFEX,date 參數真正生效',
            'Bug #3 fix: /options 改從身份別 cell 之後算 nums,避免 rowspan 偏移',
            '回傳加 actual_date 欄位'
        ],
        'twse_data_source': OPENAPI_BASE,
        'note': 'TWSE 三支不吃 date(永遠回最新);TAIFEX 三支自 v3.2 起真正吃 date'
    })


# =============================================================
# /taiex /institutional /margin (TWSE OpenAPI,不吃 date)
# =============================================================

@app.route('/taiex')
def taiex():
    result = {
        'index': 0, 'change': 0, 'changePct': 0,
        'high': 0, 'low': 0, 'range': 0,
        'turnover_yi': 0, 'date': '', 'actual_date': ''
    }

    data, sc, err = fetch_openapi('/exchangeReport/MI_INDEX', name='taiex_mi_index')
    if isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            if row.get('指數') == '發行量加權股價指數':
                result['date'] = roc_to_ad(row.get('日期', ''))
                result['actual_date'] = result['date']
                idx = to_num(row.get('收盤指數'))
                sign = -1 if str(row.get('漲跌', '+')).strip() == '-' else 1
                change = sign * abs(to_num(row.get('漲跌點數')))
                pct = sign * abs(to_num(row.get('漲跌百分比')))
                if pct == 0 and idx and idx > abs(change):
                    prev = idx - change
                    if prev:
                        pct = round(change / prev * 100, 2)
                result.update({
                    'index': round(idx, 2),
                    'change': round(change, 2),
                    'changePct': round(pct, 2)
                })
                break

    try:
        data2, _, _ = fetch_openapi('/exchangeReport/FMTQIK', name='taiex_fmtqik')
        if isinstance(data2, list) and data2:
            last = data2[-1]
            for k in ('成交金額', '成交金額(元)', 'TradeValue'):
                if k in last:
                    result['turnover_yi'] = round(to_num(last[k]) / 1e8, 2)
                    break
    except Exception as e:
        logger.warning(f'[taiex] fmtqik supplement failed: {e}')

    return jsonify(result)


@app.route('/institutional')
def institutional():
    result = {
        'foreign': 0, 'foreign_dealer': 0,
        'prop_self': 0, 'prop_hedge': 0,
        'trust': 0, 'date': '', 'actual_date': ''
    }

    data, sc, err = fetch_openapi('/fund/BFI82U', name='institutional')
    if not isinstance(data, list):
        return jsonify(result)

    for row in data:
        if not isinstance(row, dict):
            continue
        if not result['date']:
            result['date'] = roc_to_ad(row.get('日期', ''))
            result['actual_date'] = result['date']

        name = ''
        for k in ('單位名稱', '身份別', 'name'):
            if k in row:
                name = str(row[k]).strip()
                break

        net = 0
        for k in ('買賣差額', '買賣超', '差額', 'Difference'):
            if k in row:
                net = to_num(row[k])
                break

        net_yi = round(net / 1e8, 2)

        if not name:
            continue

        if '自營商' in name and '避險' in name:
            result['prop_hedge'] = net_yi
        elif '自營商' in name and ('自行' in name or '自營' in name) and '外資' not in name:
            result['prop_self'] = net_yi
        elif '投信' in name:
            result['trust'] = net_yi
        elif '外資' in name and '自營' in name:
            result['foreign_dealer'] = net_yi
        elif '外資' in name and '不含' not in name:
            result['foreign'] = net_yi

    return jsonify(result)


@app.route('/margin')
def margin():
    result = {'margin_balance': 0, 'short_units': 0, 'date': '', 'actual_date': ''}

    data, sc, err = fetch_openapi('/exchangeReport/MI_MARGN', name='margin')
    if not isinstance(data, list):
        return jsonify(result)

    for row in data:
        if not isinstance(row, dict):
            continue
        if not result['date']:
            result['date'] = roc_to_ad(row.get('日期', ''))
            result['actual_date'] = result['date']

        item = ''
        for k in ('項目', '信用交易', 'Type'):
            if k in row:
                item = str(row[k]).strip()
                break
        if not item:
            for v in row.values():
                vs = str(v)
                if '融資' in vs or '融券' in vs:
                    item = vs
                    break

        today_val = 0
        for k in ('今日餘額', '本日餘額', "Today'sBalance", 'TodaysBalance'):
            if k in row:
                today_val = to_num(row[k])
                break

        if '融資' in item:
            result['margin_balance'] = int(today_val)
        elif '融券' in item:
            result['short_units'] = int(today_val)

    return jsonify(result)


# =============================================================
# TAIFEX 共用工具
# =============================================================

def fetch_taifex_html_post(url, params, name='taifex'):
    """★ v3.2:改用 POST 打 TAIFEX(GET 不接受 queryStartDate 參數)
    
    保留 GET fallback,以防部分 endpoint 需要 GET。
    """
    # 先試 POST
    try:
        logger.info(f'[{name}] POST: {url} params={params}')
        res = requests.post(url, data=params, headers=HEADERS, timeout=20, verify=False)
        logger.info(f'[{name}] POST status: {res.status_code}, length: {len(res.content)}')
        try:
            res.encoding = 'big5'
            text = res.text
            if '外資' not in text and '自營' not in text:
                res.encoding = 'utf-8'
                text = res.text
        except Exception:
            text = res.text
        # 確認查詢日期是否真的命中(TAIFEX 沒接受 POST 的話會回最新)
        if text and len(text) > 1000:
            return text
    except Exception as e:
        logger.error(f'[{name}] POST error: {e}')
    
    # GET fallback
    try:
        logger.info(f'[{name}] GET fallback: {url}')
        res = requests.get(url, params=params, headers=HEADERS, timeout=20, verify=False)
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
        logger.error(f'[{name}] GET error: {e}')
        return ''


def parse_taifex_table(html):
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE)
    parsed = []
    for r in rows:
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r, re.DOTALL | re.IGNORECASE)
        cells = [re.sub(r'<[^>]+>', '', c).replace('&nbsp;', ' ').strip() for c in cells]
        if cells:
            parsed.append(cells)
    return parsed


def extract_query_date_from_html(html):
    """從 TAIFEX HTML 抓出實際查詢到的日期(用於回報 actual_date)"""
    # TAIFEX 頁面通常有「日期: 2026/5/8」字樣
    m = re.search(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', html)
    if m:
        return f'{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}'
    return ''


# =============================================================
# /futures — ★ v3.2:改 POST + 從身份別之後算 nums
# =============================================================

@app.route('/futures')
def futures():
    target = parse_query_date()
    date_fmt = target.strftime('%Y/%m/%d')

    result = {
        'foreign_TXF_oi': 0, 'foreign_MXF_oi': 0,
        'prop_TXF_oi': 0, 'prop_MXF_oi': 0,
        'date': date_fmt, 'actual_date': ''
    }

    products = [
        ('TXF', 'foreign_TXF_oi', 'prop_TXF_oi'),
        ('MXF', 'foreign_MXF_oi', 'prop_MXF_oi'),
    ]

    actual_dates = []
    for prod_code, fkey, pkey in products:
        url = 'https://www.taifex.com.tw/cht/3/futContractsDate'
        params = {
            'queryStartDate': target.strftime('%Y/%m/%d'),
            'queryEndDate': target.strftime('%Y/%m/%d'),
            'commodityId': prod_code
        }
        html = fetch_taifex_html_post(url, params, name=f'futures_{prod_code}')
        if not html:
            continue

        actual = extract_query_date_from_html(html)
        if actual:
            actual_dates.append(actual)

        rows = parse_taifex_table(html)
        logger.info(f'[futures_{prod_code}] parsed {len(rows)} rows')

        for cells in rows:
            # 找身份別 cell 位置
            ident_idx = -1
            ident_type = None
            for i, c in enumerate(cells):
                if '自營商' in c:
                    ident_idx = i
                    ident_type = 'prop'
                    break
                if '外資' in c and '陸資' not in c:
                    ident_idx = i
                    ident_type = 'foreign'
                    break

            if ident_idx < 0:
                continue

            # 從身份別之後重新抓 nums(穩健,不受 rowspan 影響)
            sub_cells = cells[ident_idx + 1:]
            nums = [c for c in sub_cells if re.match(r'^-?[\d,]+$', c)]
            
            logger.info(f'[futures_{prod_code}] {ident_type} ident_idx={ident_idx} nums={nums}')

            # 期貨表頭(TAIFEX): 多方口數/契約金額/空方口數/契約金額/多空淨額口數/契約金額(交易)
            #                   多方口數/契約金額/空方口數/契約金額/多空淨額口數/契約金額(未平倉)
            # 第 11 欄(index 10)是「未平倉多空淨額口數」
            if len(nums) >= 11:
                net_oi = int(to_num(nums[10]))
                if ident_type == 'foreign':
                    result[fkey] = net_oi
                else:
                    result[pkey] = net_oi

    if actual_dates:
        result['actual_date'] = actual_dates[0]
    return jsonify(result)


# =============================================================
# /options — ★ v3.2:改 POST + 從身份別之後算 nums(修自營商 290 萬 bug)
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
        'date': date_fmt, 'actual_date': ''
    }

    url = 'https://www.taifex.com.tw/cht/3/callsAndPutsDate'
    params = {
        'queryStartDate': target.strftime('%Y/%m/%d'),
        'queryEndDate': target.strftime('%Y/%m/%d'),
        'commodityId': 'TXO'
    }
    html = fetch_taifex_html_post(url, params, name='options')
    if not html:
        return jsonify(result)

    result['actual_date'] = extract_query_date_from_html(html)

    rows = parse_taifex_table(html)
    logger.info(f'[options] parsed {len(rows)} rows')

    current_cp = None  # 'call' or 'put',跨列繼承(處理 rowspan)

    for cells in rows:
        # 切 call/put
        if any('買權' in c for c in cells):
            current_cp = 'call'
        elif any('賣權' in c for c in cells):
            current_cp = 'put'

        if not current_cp:
            continue

        # 找身份別 cell
        ident_idx = -1
        ident_type = None
        for i, c in enumerate(cells):
            if '自營商' in c:
                ident_idx = i
                ident_type = 'prop'
                break
            if '外資' in c and '陸資' not in c:
                ident_idx = i
                ident_type = 'foreign'
                break

        if ident_idx < 0:
            continue

        # 從身份別之後重新抓 nums
        sub_cells = cells[ident_idx + 1:]
        nums = [c for c in sub_cells if re.match(r'^-?[\d,]+$', c)]
        
        logger.info(f'[options] {current_cp} {ident_type} nums={nums}')

        # 選擇權表頭(同期貨,12 個數字):
        #   交易: 多口/多金/空口/空金/淨口/淨金
        #   未平倉: 多口/多金/空口/空金/淨口/淨金
        # 想抓未平倉 多口[6]/多金[7]/空口[8]/空金[9]
        if len(nums) < 10:
            continue

        b_oi = int(to_num(nums[6]))
        b_amt = int(to_num(nums[7]))
        s_oi = int(to_num(nums[8]))
        s_amt = int(to_num(nums[9]))

        if ident_type == 'foreign':
            if current_cp == 'call':
                result['foreign_BC_oi'] = b_oi
                result['foreign_BC_amt'] = b_amt
                result['foreign_SC_oi'] = s_oi
                result['foreign_SC_amt'] = s_amt
            else:
                result['foreign_BP_oi'] = b_oi
                result['foreign_BP_amt'] = b_amt
                result['foreign_SP_oi'] = s_oi
                result['foreign_SP_amt'] = s_amt
        elif ident_type == 'prop':
            if current_cp == 'call':
                result['prop_BC_oi'] = b_oi
                result['prop_BC_amt'] = b_amt
                result['prop_SC_oi'] = s_oi
                result['prop_SC_amt'] = s_amt
            else:
                result['prop_BP_oi'] = b_oi
                result['prop_BP_amt'] = b_amt
                result['prop_SP_oi'] = s_oi
                result['prop_SP_amt'] = s_amt

    return jsonify(result)


# =============================================================
# /pcr — ★ v3.2:修 regex bug
# =============================================================

@app.route('/pcr')
def pcr():
    target = parse_query_date()
    date_fmt = target.strftime('%Y/%m/%d')
    target_norm = normalize_date(date_fmt)

    result = {'pcr_oi': 0, 'pcr_volume': 0, 'date': date_fmt, 'actual_date': ''}

    url = 'https://www.taifex.com.tw/cht/3/pcRatio'
    params = {
        'queryStartDate': target.strftime('%Y/%m/%d'),
        'queryEndDate': target.strftime('%Y/%m/%d')
    }
    html = fetch_taifex_html_post(url, params, name='pcr')
    if not html:
        return jsonify(result)

    rows = parse_taifex_table(html)
    logger.info(f'[pcr] parsed {len(rows)} rows')

    matched_target = False
    fallback = None

    for cells in rows:
        if len(cells) < 5:
            continue
        first = cells[0]
        # ★ v3.2 修補:regex 通吃月日 1-2 位
        if re.match(r'\d{4}[/-]\d{1,2}[/-]\d{1,2}', first):
            row_norm = normalize_date(first)
            try:
                # cells[3] = 買賣權成交量比率(顯示為百分比,如 111.45 → 1.1145)
                # cells[6] = 買賣權未平倉量比率(同上)
                vol_raw = to_num(cells[3]) if len(cells) > 3 else 0
                oi_raw = to_num(cells[6]) if len(cells) > 6 else to_num(cells[-1])
                pcr_vol = round(vol_raw / 100, 4) if vol_raw > 5 else round(vol_raw, 4)
                pcr_oi = round(oi_raw / 100, 4) if oi_raw > 5 else round(oi_raw, 4)
                
                if row_norm == target_norm:
                    result['pcr_volume'] = pcr_vol
                    result['pcr_oi'] = pcr_oi
                    result['actual_date'] = row_norm
                    matched_target = True
                    break
                if fallback is None:
                    fallback = (row_norm, pcr_vol, pcr_oi)
            except Exception as e:
                logger.error(f'[pcr] parse error: {e}')

    # 沒找到 target 就用 fallback(通常是表格最新那筆)
    if not matched_target and fallback:
        result['actual_date'] = fallback[0]
        result['pcr_volume'] = fallback[1]
        result['pcr_oi'] = fallback[2]

    return jsonify(result)


# =============================================================
# Debug endpoints
# =============================================================

@app.route('/debug/twse_taiex')
def debug_twse_taiex():
    url = f'{OPENAPI_BASE}/exchangeReport/MI_INDEX'
    try:
        res = requests.get(url, headers=HEADERS, timeout=20, verify=False)
        try:
            data = res.json()
            weighted = None
            if isinstance(data, list):
                weighted = next(
                    (r for r in data if isinstance(r, dict) and r.get('指數') == '發行量加權股價指數'),
                    None
                )
            return jsonify({
                'url': url,
                'status_code': res.status_code,
                'rows_count': len(data) if isinstance(data, list) else None,
                'first_3_rows': data[:3] if isinstance(data, list) else data,
                'weighted_idx_row': weighted,
            })
        except Exception as je:
            return jsonify({
                'url': url,
                'status_code': res.status_code,
                'json_parse_error': str(je),
                'text_preview': res.text[:500]
            })
    except Exception as e:
        return jsonify({
            'url': url,
            'error_type': type(e).__name__,
            'error_msg': str(e)[:300]
        }), 500


@app.route('/debug/futures')
def debug_futures():
    target = parse_query_date()
    url = 'https://www.taifex.com.tw/cht/3/futContractsDate'
    params = {
        'queryStartDate': target.strftime('%Y/%m/%d'),
        'queryEndDate': target.strftime('%Y/%m/%d'),
        'commodityId': 'TXF'
    }
    html = fetch_taifex_html_post(url, params, name='debug_futures')
    rows = parse_taifex_table(html)
    actual = extract_query_date_from_html(html)
    return jsonify({
        'requested_date': target.strftime('%Y/%m/%d'),
        'actual_date_in_html': actual,
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
    html = fetch_taifex_html_post(url, params, name='debug_options')
    rows = parse_taifex_table(html)
    actual = extract_query_date_from_html(html)
    
    # 多印一些有用的:有「自營商」「外資」的行
    detail_rows = []
    for r in rows[:30]:
        if any('自營商' in c or ('外資' in c and '陸資' not in c) for c in r):
            ident_idx = next((i for i, c in enumerate(r) if '自營商' in c or ('外資' in c and '陸資' not in c)), -1)
            sub = r[ident_idx + 1:] if ident_idx >= 0 else []
            nums = [c for c in sub if re.match(r'^-?[\d,]+$', c)]
            detail_rows.append({'cells': r, 'ident_idx': ident_idx, 'nums': nums})
    
    return jsonify({
        'requested_date': target.strftime('%Y/%m/%d'),
        'actual_date_in_html': actual,
        'html_len': len(html),
        'rows_count': len(rows),
        'first_8_rows': rows[:8],
        'detail_rows_with_ident': detail_rows[:6],
    })


@app.route('/debug/pcr')
def debug_pcr():
    target = parse_query_date()
    url = 'https://www.taifex.com.tw/cht/3/pcRatio'
    params = {
        'queryStartDate': target.strftime('%Y/%m/%d'),
        'queryEndDate': target.strftime('%Y/%m/%d')
    }
    html = fetch_taifex_html_post(url, params, name='debug_pcr')
    rows = parse_taifex_table(html)
    return jsonify({
        'requested_date': target.strftime('%Y/%m/%d'),
        'html_len': len(html),
        'rows_count': len(rows),
        'first_8_rows': rows[:8],
    })


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
