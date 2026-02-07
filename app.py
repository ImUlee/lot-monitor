from flask import Flask, request, jsonify, render_template, send_from_directory
import sqlite3
import re
import os
import sys
import time
from datetime import datetime, timedelta

app = Flask(__name__)

app.jinja_env.variable_start_string = '[['
app.jinja_env.variable_end_string = ']]'

DB_PATH = '/app/data/lottery.db'
LAST_HEARTBEAT = 0 

# 月份映射表
MONTH_MAP = {
    'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
}

def parse_log_date(date_str):
    """
    万能日期解析函数
    支持: 
    - 06/Feb/2026 23:51:08
    - 2026-02-06 23:51:08
    - 2026/02/06 23:51:08
    """
    try:
        date_str = date_str.strip()
        # 尝试格式 1: 06/Feb/2026 23:51:08
        if '/' in date_str and re.search(r'[a-zA-Z]', date_str):
            parts = date_str.split()
            d_parts = parts[0].split('/')
            t_parts = parts[1].split(':')
            day = int(d_parts[0])
            month = MONTH_MAP.get(d_parts[1], 0)
            year = int(d_parts[2])
            return datetime(year, month, day, int(t_parts[0]), int(t_parts[1]), int(t_parts[2]))
        
        # 尝试格式 2: 2026-02-06 23:51:08 (标准 ISO)
        if '-' in date_str:
            return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
            
        # 尝试格式 3: 2026/02/06 23:51:08
        if '/' in date_str:
            return datetime.strptime(date_str, "%Y/%m/%d %H:%M:%S")

        # 尝试格式 4: 2026.02.06 23:51:08
        if '.' in date_str:
            return datetime.strptime(date_str, "%Y.%m.%d %H:%M:%S")

        return None
    except Exception as e:
        print(f"[Date Parse Fail] '{date_str}': {e}", flush=True)
        return None

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, log_time TEXT, nickname TEXT, item_type TEXT, quantity INTEGER, unique_sign TEXT UNIQUE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_overrides (date TEXT PRIMARY KEY, manual_users INTEGER, manual_sum INTEGER)''')
    conn.commit()
    conn.close()

init_db()

@app.route('/manifest.json')
def serve_manifest(): return send_from_directory('static', 'manifest.json', mimetype='application/json')

@app.route('/sw.js')
def serve_sw(): return send_from_directory('static', 'sw.js', mimetype='application/javascript')

@app.route('/static/<path:path>')
def send_static(path): return send_from_directory('static', path)

@app.route('/')
def dashboard(): return render_template('dashboard.html')

@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    global LAST_HEARTBEAT
    LAST_HEARTBEAT = time.time()
    return jsonify({"status": "ok"})

@app.route('/api/health', methods=['GET'])
def health_check(): return jsonify({"status": "online", "server": "LotMonitor"})

@app.route('/api/update_history', methods=['POST'])
def update_history():
    data = request.json
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("REPLACE INTO daily_overrides (date, manual_users, manual_sum) VALUES (?, ?, ?)", (data.get('date'), data.get('manual_users'), data.get('manual_sum')))
        conn.commit()
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "msg": str(e)}), 500
    finally: conn.close()

@app.route('/api/update_log', methods=['POST'])
def update_log():
    data = request.json
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("UPDATE logs SET nickname = ?, quantity = ? WHERE id = ?", (data.get('nickname'), data.get('quantity'), data.get('id')))
        conn.commit()
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "msg": str(e)}), 500
    finally: conn.close()

@app.route('/upload', methods=['POST'])
def upload_file():
    sys.stdout.flush()
    file = request.files.get('file')
    if not file: return jsonify({"status": "error"}), 400
    raw_data = file.read()
    try: content = raw_data.decode('gb18030')
    except: content = raw_data.decode('utf-8', errors='ignore')
    
    lines = content.split('\n')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    new_count = 0
    # 宽松正则
    pattern = r"\[(.*?)\]\s+(.*?)_\d+\s+\|.*?[,，]\s*(?:.*?)[,，]\s*(\d+)"
    for line in lines:
        line = line.strip()
        if not line: continue 
        match = re.search(pattern, line)
        if match:
            log_time = match.group(1)
            nickname = match.group(2)
            quantity = int(match.group(3))
            unique_sign = f"{log_time}_{nickname}_{quantity}"
            try:
                c.execute("INSERT INTO logs (log_time, nickname, item_type, quantity, unique_sign) VALUES (?, ?, ?, ?, ?)", (log_time, nickname, "钻石", quantity, unique_sign))
                new_count += 1
            except sqlite3.IntegrityError: pass 
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "new_entries": new_count})

@app.route('/api/stats')
def get_stats():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    try:
        is_online = (time.time() - LAST_HEARTBEAT) < 10

        # --- A. 总览页数据 ---
        c.execute("SELECT id, log_time, nickname, quantity FROM logs")
        all_raw_logs = [dict(row) for row in c.fetchall()]

        now = datetime.now()
        cutoff_time = now - timedelta(hours=48)
        
        overview_logs = []
        for log in all_raw_logs:
            # 使用万能解析
            log_dt = parse_log_date(log['log_time'])
            if log_dt and log_dt >= cutoff_time:
                overview_logs.append({
                    "nickname": log['nickname'],
                    "quantity": log['quantity'],
                    "log_dt": log_dt
                })

        total_users = len(set(l['nickname'] for l in overview_logs))
        total_wins = sum(l['quantity'] for l in overview_logs)
        
        rank_map = {}
        for l in overview_logs:
            if l['nickname'] not in rank_map: rank_map[l['nickname']] = {"win_times": 0, "win_sum": 0}
            rank_map[l['nickname']]["win_times"] += 1
            rank_map[l['nickname']]["win_sum"] += l['quantity']
        
        rank_list = [{"nickname": k, "win_times": v["win_times"], "win_sum": v["win_sum"]} for k, v in rank_map.items()]
        rank_list.sort(key=lambda x: x['win_sum'], reverse=True)

        date_range_str = ""
        if overview_logs:
            overview_logs.sort(key=lambda x: x['log_dt'])
            # 格式化日期显示为 2026.02.06
            s_str = overview_logs[0]['log_dt'].strftime("%Y.%m.%d")
            e_str = overview_logs[-1]['log_dt'].strftime("%Y.%m.%d")
            date_range_str = s_str if s_str == e_str else f"{s_str} - {e_str}"
        else:
            date_range_str = "无近48h数据"

        # --- B. 明细页数据 ---
        c.execute("SELECT id, log_time, nickname, item_type, quantity FROM logs ORDER BY id DESC LIMIT 2000")
        details = [dict(row) for row in c.fetchall()]

        # --- C. 历史页数据 ---
        # 截取前10位作为日期分组 (例如 '06/Feb/2026' 或 '2026-02-06')
        c.execute('''
            SELECT substr(l.log_time, 1, 10) as date_str, COUNT(DISTINCT l.nickname) as calc_users, SUM(l.quantity) as calc_sum, d.manual_users, d.manual_sum
            FROM logs l LEFT JOIN daily_overrides d ON substr(l.log_time, 1, 10) = d.date GROUP BY date_str 
        ''')
        raw_history = c.fetchall()
        history_list = []
        for row in raw_history:
            final_users = row['manual_users'] if row['manual_users'] is not None else row['calc_users']
            final_sum = row['manual_sum'] if row['manual_sum'] is not None else row['calc_sum']
            
            # 排序解析
            dt = parse_log_date(row['date_str'] + " 00:00:00")
            sort_key = dt if dt else datetime.min

            history_list.append({
                "date": row['date_str'],
                "user_count": final_users,
                "daily_sum": final_sum,
                "is_manual": row['manual_users'] is not None,
                "sort_key": sort_key
            })
        
        # 倒序排列，最新的在最上面
        history_list.sort(key=lambda x: x['sort_key'], reverse=True)

    except Exception as e:
        print(f"Stats Error: {e}", flush=True)
        total_users, total_wins, rank_list, details, history_list = 0, 0, [], [], []
        date_range_str = "Error"
        is_online = False
    
    conn.close()
    return jsonify({
        "client_status": "在线" if is_online else "离线",
        "total_users": total_users,
        "total_wins": total_wins,
        "rank_list": rank_list,
        "date_range": date_range_str,
        "details": details,
        "history_data": history_list
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)