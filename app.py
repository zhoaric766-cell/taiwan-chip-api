from flask import Flask, jsonify
import requests
from datetime import datetime, timedelta
import re

app = Flask(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Cache-Control': 'no-cache'
}

def get_target_date(date_str=None):
    if date_str:
        try:
            return datetime.strptime(date_str, '%Y-%m-%d')
        except:
            pass
    d = datetime.now()
    for _ in range(7):
        if d.weekday() < 5:
            return d
        d -= timedelta(days=1)
    return datetime.now()

def to_num(v):
    if v is None or v == '' or v == '-' or v == '--':
        return 0
    s = str(v).replace(',', '').strip()
    try:
        return float(s)
    except:
        return 0

@app.route('/')
def index():
    return jsonify({
        'service': '台股籌碼 API 中介服務',
        'version': 'v1.0',
        'endpoints': [
            '/futures?date=YYYY-MM-DD',
            '/options?date=YYYY-MM-DD',
            '/pcr?date=YYYY-MM-DD',
            '/taiex?date=YYYY-MM-DD',
            '/margin?date=YYYY-MM-DD',
            '/institutional?date=YYYY-MM-DD',
            '/all?date=YYYY-MM-DD'
        ]
    })

@app.route('/futures')
def futures():
    from flask import request
    date_str = request.args.get('date')
    target = get_target_date(date_str)
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
        try:
            url = 'https://www.taifex.com.tw/cht/3/futContractsDate'
            params = {
                'queryStartDate': target.strftime('%Y/%m/%d'),
                'queryEndDate': target.strftime('%Y/%m/%d'),
                'commodityId': prod_code
            }
            res = requests.get(url, params=params, headers=HEADERS, timeout=15)
            res.encoding = 'big5'
            text = res.text
            
            rows = re.findall(r'<tr[^>]*>.*?</tr>', text, re.DOTALL)
            for row in rows:
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
                if len(cells) < 10:
                    continue
                role = cells[0] if cells else ''
                if '外資' in role and '自營' not in role:
                    long_oi = to_num(cells[5]) if len(cells) > 5 else 0
                    short_oi = to_num(cells[8]) if len(cells) > 8 else 0
                    result[fkey] = int(long_oi - short_oi)
                elif '自營商' in role:
                    long_oi = to_num(cells[5]) if len(cells) > 5 else 0
                    short_oi = to_num(cells[8]) if len(cells) > 8 else 0
                    result[pkey] = int(long_oi - short_oi)
        except Exception as e:
            app.logger.error(f'futures {prod_code} error: {e}')
    
    return jsonify(result)

@app.route('/options')
def options():
    from flask import request
    date_str = request.args.get('date')
    target = get_target_date(date_str)
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
    
    try:
        url = 'https://www.taifex.com.tw/cht/3/callsAndPutsDate'
        params = {
            'queryStartDate': target.strftime('%Y/%m/%d'),
            'queryEndDate': target.strftime('%Y/%m/%d'),
            'commodityId': 'TXO'
        }
        res = requests.get(url, params=params, headers=HEADERS, timeout=15)
        res.encoding = 'big5'
        text = res.text
        
        rows = re.findall(r'<tr[^>]*>.*?</tr>', text, re.DOTALL)
        current_cp = None
        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
            if not cells:
                continue
            first = cells[0]
            if '買權' in first or 'Call' in first:
                current_cp = 'call'
            elif '賣權' in first or 'Put' in first:
                current_cp = 'put'
            
            role = ''
            for c in cells:
                if '外資' in c and '自營' not in c:
                    role = 'foreign'
                    break
                elif '自營商' in c:
                    role = 'prop'
                    break
            
            if not role or not current_cp:
                continue
            
            try:
                long_oi  = int(to_num(cells[3])) if len(cells) > 3 else 0
                long_amt = int(to_num(cells[4])) if len(cells) > 4 else 0
                short_oi = int(to_num(cells[6])) if len(cells) > 6 else 0
                short_amt= int(to_num(cells[7])) if len(cells) > 7 else 0
                
                if role == 'foreign' and current_cp == 'call':
                    result['foreign_BC_oi'] = long_oi
                    result['foreign_BC_amt'] = long_amt
                    result['foreign_SC_oi'] = short_oi
                    result['foreign_SC_amt'] = short_amt
                elif role == 'foreign' and current_cp == 'put':
                    result['foreign_BP_oi'] = long_oi
                    result['foreign_BP_amt'] = long_amt
                    result['foreign_SP_oi'] = short_oi
                    result['foreign_SP_amt'] = short_amt
                elif role == 'prop' and current_cp == 'call':
                    result['prop_BC_oi'] = long_oi
                    result['prop_BC_amt'] = long_amt
                    result['prop_SC_oi'] = short_oi
                    result['prop_SC_amt'] = short_amt
                elif role == 'prop' and current_cp == 'put':
                    result['prop_BP_oi'] = long_oi
                    result['prop_BP_amt'] = long_amt
                    result['prop_SP_oi'] = short_oi
                    result['prop_SP_amt'] = short_amt
            except Exception as e:
                app.logger.error(f'options parse row error: {e}')
    
    except Exception as e:
        app.logger.error(f'options error: {e}')
    
    return jsonify(result)

@app.route('/pcr')
def pcr():
    from flask import request
    date_str = request.args.get('date')
    target = get_target_date(date_str)
    date_fmt = target.strftime('%Y/%m/%d')
    
    result = {'pcr_oi': 0, 'pcr_volume': 0, 'date': date_fmt}
    
    try:
        url = 'https://www.taifex.com.tw/cht/3/pcRatio'
        res = requests.get(url, headers=HEADERS, timeout=15)
        res.encoding = 'big5'
        text = res.text
        
        rows = re.findall(r'<tr[^>]*>.*?</tr>', text, re.DOTALL)
        target_str = target.strftime('%Y/%m/%d')
        
        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
            if len(cells) >= 5 and target_str in cells[0]:
                result['pcr_volume'] = round(to_num(cells[1]) / 100, 4)
                result['pcr_oi'] = round(to_num(cells[4]) / 100, 4)
                break
        
        if result['pcr_oi'] == 0 and rows:
            for row in rows:
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
                if len(cells) >= 5 and re.match(r'\d{4}/\d{2}/\d{2}', cells[0]):
                    result['pcr_volume'] = round(to_num(cells[1]) / 100, 4)
                    result['pcr_oi'] = round(to_num(cells[4]) / 100, 4)
                    result['date'] = cells[0]
                    break
    
    except Exception as e:
        app.logger.error(f'pcr error: {e}')
    
    return jsonify(result)

@app.route('/taiex')
def taiex():
    from flask import request
    date_str = request.args.get('date')
    target = get_target_date(date_str)
    date_fmt = target.strftime('%Y/%m/%d')
    date_compact = target.strftime('%Y%m%d')
    
    result = {
        'index': 0, 'change': 0, 'changePct': 0,
        'high': 0, 'low': 0, 'range': 0,
        'turnover_yi': 0, 'date': date_fmt
    }
    
    try:
        url = f'https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={date_compact}&response=json&_={int(datetime.now().timestamp()*1000)}'
        res = requests.get(url, headers=HEADERS, timeout=15)
        data = res.json()
        
        tables = data.get('tables', [])
        rows = tables[0].get('data', []) if tables else data.get('data', [])
        
        roc_date = str(target.year - 1911) + '/' + target.strftime('%m/%d')
        
        for row in rows:
            if len(row) >= 6 and (roc_date in str(row[0]) or str(row[0]).replace(' ', '') in roc_date.replace(' ', '')):
                turnover = to_num(row[2])
                index = to_num(row[4])
                change = to_num(row[5])
                prev = index - change
                pct = round(change / prev * 100, 2) if prev else 0
                result.update({
                    'index': round(index, 2),
                    'change': round(change, 2),
                    'changePct': pct,
                    'turnover_yi': round(turnover / 100000000, 2)
                })
                break
        
        url2 = f'https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date={date_compact}&response=json&_={int(datetime.now().timestamp()*1000)}'
        res2 = requests.get(url2, headers=HEADERS, timeout=15)
        data2 = res2.json()
        tables2 = data2.get('tables', [])
        rows2 = tables2[0].get('data', []) if tables2 else data2.get('data', [])
        for row in rows2:
            if len(row) >= 4 and roc_date in str(row[0]):
                high = to_num(row[2])
                low = to_num(row[3])
                result.update({'high': high, 'low': low, 'range': round(high - low, 2)})
                break
    
    except Exception as e:
        app.logger.error(f'taiex error: {e}')
    
    return jsonify(result)

@app.route('/margin')
def margin():
    from flask import request
    date_str = request.args.get('date')
    target = get_target_date(date_str)
    date_fmt = target.strftime('%Y/%m/%d')
    date_compact = target.strftime('%Y%m%d')
    
    result = {'margin_balance': 0, 'short_units': 0, 'date': date_fmt}
    
    try:
        url = f'https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date_compact}&selectType=MS&response=json&_={int(datetime.now().timestamp()*1000)}'
        res = requests.get(url, headers=HEADERS, timeout=15)
        data = res.json()
        
        tables = data.get('tables', [])
        all_rows = []
        for t in tables:
            all_rows.extend(t.get('data', []))
        if not all_rows:
            all_rows = data.get('data', [])
        
        mb, su = 0, 0
        for row in all_rows:
            if not row:
                continue
            cat = str(row[0])
            if ('融資' in cat or '資' == cat.strip()) and len(row) >= 6:
                v = to_num(row[5])
                if v > mb:
                    mb = v
            if ('融券' in cat or '券' == cat.strip()) and len(row) >= 6:
                v = to_num(row[5])
                if v > su:
                    su = v
        
        result['margin_balance'] = mb
        result['short_units'] = su
    
    except Exception as e:
        app.logger.error(f'margin error: {e}')
    
    return jsonify(result)

@app.route('/institutional')
def institutional():
    from flask import request
    date_str = request.args.get('date')
    target = get_target_date(date_str)
    date_fmt = target.strftime('%Y/%m/%d')
    date_compact = target.strftime('%Y%m%d')
    
    result = {
        'foreign': 0, 'prop_self': 0,
        'prop_hedge': 0, 'trust': 0,
        'date': date_fmt
    }
    
    try:
        url = f'https://www.twse.com.tw/rwd/zh/fund/BFI82U?dayDate={date_compact}&type=day&response=json&_={int(datetime.now().timestamp()*1000)}'
        res = requests.get(url, headers=HEADERS, timeout=15)
        data = res.json()
        
        tables = data.get('tables', [])
        all_rows = []
        for t in tables:
            all_rows.extend(t.get('data', []))
        if not all_rows:
            all_rows = data.get('data', [])
        
        for row in all_rows:
            if not row or len(row) < 4:
                continue
            role = str(row[0]).strip()
            net = round(to_num(row[3]) / 100000000, 2)
            if '外資' in role and '自營' not in role and '陸資' not in role:
                result['foreign'] = net
            elif '外資及陸資' in role and '不含' not in role:
                result['foreign'] = net
            elif '自營商' in role and '自行' in role:
                result['prop_self'] = net
            elif '自營商' in role and '避險' in role:
                result['prop_hedge'] = net
            elif '投信' in role:
                result['trust'] = net
    
    except Exception as e:
        app.logger.error(f'institutional error: {e}')
    
    return jsonify(result)

@app.route('/all')
def all_data():
    from flask import request
    date_str = request.args.get('date')
    
    fut = futures_data(date_str)
    opt = options_data(date_str)
    pcr_d = pcr_data(date_str)
    tai = taiex_data(date_str)
    mar = margin_data(date_str)
    ins = institutional_data(date_str)
    
    return jsonify({
        'futures': fut,
        'options': opt,
        'pcr': pcr_d,
        'taiex': tai,
        'margin': mar,
        'institutional': ins
    })

def futures_data(date_str=None):
    with app.test_request_context(f'/?date={date_str or ""}'):
        return futures().get_json()

def options_data(date_str=None):
    with app.test_request_context(f'/?date={date_str or ""}'):
        return options().get_json()

def pcr_data(date_str=None):
    with app.test_request_context(f'/?date={date_str or ""}'):
        return pcr().get_json()

def taiex_data(date_str=None):
    with app.test_request_context(f'/?date={date_str or ""}'):
        return taiex().get_json()

def margin_data(date_str=None):
    with app.test_request_context(f'/?date={date_str or ""}'):
        return margin().get_json()

def institutional_data(date_str=None):
    with app.test_request_context(f'/?date={date_str or ""}'):
        return institutional().get_json()

if __name__ == '__main__':
    app.run(debug=False)
