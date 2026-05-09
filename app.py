"""
台股籌碼 API 中介服務 v3.0
============================================================
2026-05-09 重大變更:
- TWSE 三支(/taiex /institutional /margin)切換至 openapi.twse.com.tw
  → 擺脫 www.twse.com.tw 對雲端 IP 的反爬封鎖(原 v2.0 SSL 修復後仍拿空陣列)
- TAIFEX 三支(/futures /options /pcr)邏輯完全保留(已驗證可用)
- Debug 端點強化:回傳 status_code、所有 keys、完整 raw rows,診斷力大幅提升
- 新增 /debug/twse_institutional 與 /debug/twse_margin

OpenAPI 特性:
- 不吃 date 參數,永遠回「最新已公告」交易日
- response 內 'date' 欄位由 OpenAPI 自己的「日期」欄轉民國→西元而來
- 因此 ?date=YYYY-MM-DD 對 TWSE 三支不再有作用,但對 TAIFEX 三支仍生效
"""

from flask import Flask, jsonify, request
import requests
import urllib3
from datetime import datetime, timedelta
import re
import logging

# Disable SSL warnings (TAIFEX cert 仍需 verify=False;OpenAPI 不需要)
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
    """取得目標交易日(主要供 TAIFEX 使用)"""
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
    date_str = request.args.get('date')
    return get_target_date(date_str)


def roc_to_ad(roc_date_str):
    """民國日期 '1150410' → '2026/04/10'"""
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


def fetch_openapi(path, name='openapi'):
    """打 TWSE OpenAPI,回傳 (data, status_code, error_msg)"""
    url = f'{OPENAPI_BASE}{path}'
    try:
        logger.info(f'[{name}] fetching: {url}')
        res = requests.get(url, headers=HEADERS, timeout=20)
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
        'version': 'v3.0',
        'note': 'TWSE 改用 openapi.twse.com.tw(擺脫雲端 IP 反爬);TAIFEX 仍直連',
        'twse_data_source': OPENAPI_BASE,
        'note_openapi': 'OpenAPI 不吃 date 參數,永遠回最新已公告交易日;date 參數僅對 TAIFEX 生效',
        'endpoints': [
            '/taiex', '/institutional', '/margin',
            '/futures', '/options', '/pcr',
            '/all',
            '/debug/twse_taiex', '/debug/twse_institutional', '/debug/twse_margin',
            '/debug/futures', '/debug/options', '/debug/pcr'
        ]
    })


# =============================================================
# /taiex — 加權指數 (OpenAPI MI_INDEX + 嘗試 FMTQIK 補成交量)
# =============================================================

@app.route('/taiex')
def taiex():
    result = {
        'index': 0, 'change': 0, 'changePct': 0,
        'high': 0, 'low': 0, 'range': 0,
        'turnover_yi': 0, 'date': ''
    }

    # MI_INDEX:各類指數收盤
    data, sc, err = fetch_openapi('/exchangeReport/MI_INDEX', name='taiex_mi_index')
    if isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            if row.get('指數') == '發行量加權股價指數':
                logger.info(f'[taiex] matched: {row}')
                result['date'] = roc_to_ad(row.get('日期', ''))
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
    else:
        logger.error(f'[taiex] MI_INDEX failed: {err}')

    # FMTQIK:嘗試補成交金額(OpenAPI 通常回最近一段歷史)
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


# =============================================================
# /institutional — 三大法人現貨 (OpenAPI BFI82U)
# =============================================================

@app.route('/institutional')
def institutional():
    result = {
        'foreign': 0,        # 外資及陸資(不含外資自營)
        'foreign_dealer': 0, # 外資自營商(若 OpenAPI 有給就填)
        'prop_self': 0,      # 自營商(自行買賣)
        'prop_hedge': 0,     # 自營商(避險)
        'trust': 0,          # 投信
        'date': ''
    }

    data, sc, err = fetch_openapi('/fund/BFI82U', name='institutional')
    if not isinstance(data, list):
        logger.error(f'[institutional] failed: {err}')
        return jsonify(result)

    for row in data:
        if not isinstance(row, dict):
            continue
        if not result['date']:
            result['date'] = roc_to_ad(row.get('日期', ''))

        # 取單位名稱(嘗試多 key)
        name = ''
        for k in ('單位名稱', '身份別', 'name'):
            if k in row:
                name = str(row[k]).strip()
                break

        # 取買賣差額(嘗試多 key)
        net = 0
        for k in ('買賣差額', '買賣超', '差額', 'Difference'):
            if k in row:
                net = to_num(row[k])
                break

        net_yi = round(net / 1e8, 2)
        logger.info(f'[institutional] name="{name}" net_yi={net_yi}')

        if not name:
            continue

        if '自營商' in name and '避險' in name:
            result['prop_hedge'] = net_yi
        elif '自營商' in name and ('自行' in name or '自營' in name) and '外資' not in name:
            result['prop_self'] = net_yi
        elif '投信' in name:
            result['trust'] = net_yi
        elif '外資' in name and '自營' in name:
            # 「外資自營商」獨立計
            result['foreign_dealer'] = net_yi
        elif '外資' in name and '不含' not in name:
            # 「外資及陸資(不含外資自營商)」或「外資」
            result['foreign'] = net_yi

    return jsonify(result)


# =============================================================
# /margin — 融資融券 (OpenAPI MI_MARGN)
# =============================================================

@app.route('/margin')
def margin():
    result = {'margin_balance': 0, 'short_units': 0, 'date': ''}

    data, sc, err = fetch_openapi('/exchangeReport/MI_MARGN', name='margin')
    if not isinstance(data, list):
        logger.error(f'[margin] failed: {err}')
        return jsonify(result)

    for row in data:
        if not isinstance(row, dict):
            continue
        if not result['date']:
            result['date'] = roc_to_ad(row.get('日期', ''))

        # 找「項目」欄(可能是 項目 / 信用交易 / Type)
        item = ''
        for k in ('項目', '信用交易', 'Type'):
            if k in row:
                item = str(row[k]).strip()
                break
        # 萬一沒有,掃整個 row 找含「融資」/「融券」的字串
        if not item:
            for v in row.values():
                vs = str(v)
                if '融資' in vs or '融券' in vs:
                    item = vs
                    break

        # 找「今日餘額」(可能多種 key)
        today_val = 0
        for k in ('今日餘額', '本日餘額', "Today'sBalance", 'TodaysBalance'):
            if k in row:
                today_val = to_num(row[k])
                break

        logger.info(f'[margin] item="{item}" today_val={today_val}')

        if '融資' in item:
            result['margin_balance'] = int(today_val)
        elif '融券' in item:
            result['short_units'] = int(today_val)

    return jsonify(result)


# =============================================================
# TAIFEX 共用工具(原邏輯保留)
# =============================================================

def fetch_taifex_html(url, params, name='taifex'):
    try:
        logger.info(f'[{name}] fetching: {url} params={params}')
        res = requests.get(url, params=params, headers=HEADERS, timeout=20, verify=False)
        logger.info(f'[{name}] status: {res.status_code}, length: {len(res.content)}')
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
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE)
    parsed = []
    for r in rows:
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r, re.DOTALL | re.IGNORECASE)
        cells = [re.sub(r'<[^>]+>', '', c).replace('&nbsp;', ' ').strip() for c in cells]
        if cells:
            parsed.append(cells)
    return parsed


# =============================================================
# /futures — 期貨三大法人(TAIFEX) 原邏輯
# =============================================================

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
            if '自營商' in row_text:
                logger.info(f'[futures_{prod_code}] prop row: {cells}')
                nums = [c for c in cells if re.match(r'^-?[\d,]+$', c)]
                if len(nums) >= 11:
                    net_oi = int(to_num(nums[10]))
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
# /options — 選擇權三大法人(TAIFEX) 原邏輯
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

    current_cp = None

    for cells in rows:
        if any('買權' in c for c in cells):
            current_cp = 'call'
        elif any('賣權' in c for c in cells):
            current_cp = 'put'

        if not current_cp:
            continue

        is_foreign = any('外資' in c and '陸資' not in c for c in cells)
        is_prop = any('自營商' in c for c in cells)

        if not (is_foreign or is_prop):
            continue

        nums = [c for c in cells if re.match(r'^-?[\d,]+$', c)]
        logger.info(f'[options] {current_cp} {"foreign" if is_foreign else "prop"}: nums={nums}')

        if len(nums) < 10:
            continue

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
# /pcr — Put/Call Ratio(TAIFEX) 原邏輯
# =============================================================

@app.route('/pcr')
def pcr():
    target = parse_query_date()
    date_fmt = target.strftime('%Y/%m/%d')

    result = {'pcr_oi': 0, 'pcr_volume': 0, 'date': date_fmt}

    url = 'https://www.taifex.com.tw/cht/3/pcRatio'
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
        first = cells[0]
        if re.match(r'\d{4}/\d{2}/\d{2}', first):
            logger.info(f'[pcr] date row: {cells}')
            try:
                if first == target_str or result['pcr_oi'] == 0:
                    vol_raw = to_num(cells[3]) if len(cells) > 3 else 0
                    oi_raw = to_num(cells[6]) if len(cells) > 6 else to_num(cells[-1])
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
# Debug endpoints — TWSE OpenAPI 詳細診斷
# =============================================================

@app.route('/debug/twse_taiex')
def debug_twse_taiex():
    url = f'{OPENAPI_BASE}/exchangeReport/MI_INDEX'
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
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
                'data_type': type(data).__name__,
                'rows_count': len(data) if isinstance(data, list) else None,
                'first_row_keys': list(data[0].keys()) if isinstance(data, list) and data else None,
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


@app.route('/debug/twse_institutional')
def debug_twse_institutional():
    url = f'{OPENAPI_BASE}/fund/BFI82U'
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        try:
            data = res.json()
            return jsonify({
                'url': url,
                'status_code': res.status_code,
                'rows_count': len(data) if isinstance(data, list) else None,
                'first_row_keys': list(data[0].keys()) if isinstance(data, list) and data else None,
                'all_rows': data,
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


@app.route('/debug/twse_margin')
def debug_twse_margin():
    url = f'{OPENAPI_BASE}/exchangeReport/MI_MARGN'
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        try:
            data = res.json()
            return jsonify({
                'url': url,
                'status_code': res.status_code,
                'rows_count': len(data) if isinstance(data, list) else None,
                'first_row_keys': list(data[0].keys()) if isinstance(data, list) and data else None,
                'first_10_rows': data[:10] if isinstance(data, list) else data,
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


# =============================================================
# Debug endpoints — TAIFEX(原邏輯保留)
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
