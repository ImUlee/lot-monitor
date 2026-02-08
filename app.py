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

MONTH_MAP = {
    'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
}

def parse_log_date(date_str):
    try:
        date_str = date_str.strip()
        if '/' in date_str and re.search(r'[a-zA-Z]', date_str):
            parts = date_str.split()
            d_parts = parts[0].split('/')
            t_parts = parts[1].split(':')
            return datetime(int(d_parts[2]), MONTH_MAP.get(d_parts[1], 0), int(d_parts[0]), int(t_parts[0]), int(t_parts[1]), int(t_parts[2]))
        if '-' in date_str: return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        if '/' in date_str: return datetime.strptime(date_str, "%Y/%m/%d %H:%M:%S")
        if '.' in date_str: return datetime.strptime(date_str, "%Y.%m.%d %H:%M:%S")
        return None
    except: return None

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 1. 日志数据表
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        log_time TEXT, 
        nickname TEXT, 
        item_type TEXT, 
        quantity INTEGER, 
        unique_sign TEXT UNIQUE,
        device_id TEXT
    )''')
    
    # 2. 设备状态表 (device_id为主键，确保昵称实时更新)
    c.execute('''CREATE TABLE IF NOT EXISTS devices (
        device_id TEXT PRIMARY KEY,
        nickname TEXT,
        last_seen REAL,
        process_running INTEGER
    )''')

    # 3. 历史修正表
    c.execute('''CREATE TABLE IF NOT EXISTS daily_overrides (
        date TEXT, 
        device_id TEXT,
        manual_users INTEGER, 
        manual_sum INTEGER,
        PRIMARY KEY (date, device_id)
    )''')
    
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

# 🔥 获取节点列表
@app.route('/api/nodes')
def get_nodes():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM devices ORDER BY last_seen DESC")
    rows = c.fetchall()
    nodes = []
    now = time.time()
    for r in rows:
        # 判断是否在线 (15秒超时)
        is_online = (now - r['last_seen']) < 15
        nodes.append({
            "device_id": r['device_id'],
            "nickname": r['nickname'],
            "is_online": is_online,
            "process_running": bool(r['process_running'])
        })
    conn.close()
    return jsonify({"nodes": nodes})

# 🔥 心跳接口
@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    data = request.json
    device_id = data.get('device_id')
    nickname = data.get('nickname', 'Unknown')
    process_running = 1 if data.get('process_running', False) else 0
    
    if not device_id: return jsonify({"status": "error"}), 400
    
    conn = sqlite3.connect(DB_PATH)
    try:
        # REPLACE INTO 会根据 device_id 更新昵称和状态
        conn.execute("REPLACE INTO devices (device_id, nickname, last_seen, process_running) VALUES (?, ?, ?, ?)", 
                     (device_id, nickname, time.time(), process_running))
        conn.commit()
        return jsonify({"status": "ok"})
    except Exception as e: return jsonify({"error": str(e)}), 500
    finally: conn.close()

@app.route('/api/health', methods=['GET'])
def health_check(): return jsonify({"status": "online", "server": "LittlePilot"})

@app.route('/api/update_history', methods=['POST'])
def update_history():
    data = request.json
    device_id = data.get('device_id')
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("REPLACE INTO daily_overrides (date, device_id, manual_users, manual_sum) VALUES (?, ?, ?, ?)", 
                     (data.get('date'), device_id, data.get('manual_users'), data.get('manual_sum')))
        conn.commit()
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "msg": str(e)}), 500
    finally: conn.close()

@app.route('/upload', methods=['POST'])
def upload_file():
    sys.stdout.flush()
    file = request.files.get('file')
    device_id = request.form.get('device_id')
    nickname = request.form.get('nickname', 'Unknown')
    
    if not file or not device_id: return jsonify({"status": "error"}), 400
    
    # 上传时也更新在线状态，防止大文件传输导致心跳超时
    conn = sqlite3.connect(DB_PATH)
    conn.execute("REPLACE INTO devices (device_id, nickname, last_seen, process_running) VALUES (?, ?, ?, ?)", 
                 (device_id, nickname, time.time(), 1))
    
    raw_data = file.read()
    try: content = raw_data.decode('gb18030')
    except: content = raw_data.decode('utf-8', errors='ignore')
    
    lines = content.split('\n')
    c = conn.cursor()
    new_count = 0
    pattern = r"\[(.*?)\]\s+(.*?)_\d+\s+\|.*?[,，]\s*(?:.*?)[,，]\s*(\d+)"
    
    for line in lines:
        line = line.strip()
        if not line: continue 
        match = re.search(pattern, line)
        if match:
            log_time, nick, quantity = match.group(1), match.group(2), int(match.group(3))
            unique_sign = f"{log_time}_{nick}_{quantity}_{device_id}" 
            try:
                c.execute("INSERT INTO logs (log_time, nickname, item_type, quantity, unique_sign, device_id) VALUES (?, ?, ?, ?, ?, ?)", 
                          (log_time, nick, "钻石", quantity, unique_sign, device_id))
                new_count += 1
            except sqlite3.IntegrityError: pass 
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "new_entries": new_count})

@app.route('/api/stats')
def get_stats():
    target_node_id = request.args.get('node_id')
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    try:
        # 🔥 核心逻辑修正：综合判断在线状态和进程状态
        process_status_text = "未连接"
        is_client_online = False
        
        if target_node_id:
            c.execute("SELECT last_seen, process_running FROM devices WHERE device_id = ?", (target_node_id,))
            row = c.fetchone()
            if row:
                is_client_online = (time.time() - row['last_seen']) < 15
                if not is_client_online:
                    process_status_text = "离线" # 客户端本身不在线
                elif row['process_running']:
                    process_status_text = "运行中" # 客户端在线且EXE在跑
                else:
                    process_status_text = "未运行" # 客户端在线但EXE没跑
            else:
                process_status_text = "未知设备"
        else:
            process_status_text = "请选择节点"

        # --- A. 总览页数据 ---
        query = "SELECT id, log_time, nickname, quantity FROM logs"
        params = []
        if target_node_id:
            query += " WHERE device_id = ?"
            params.append(target_node_id)
            
        c.execute(query, params)
        all_raw_logs = [dict(row) for row in c.fetchall()]

        now = datetime.now()
        cutoff_time = now - timedelta(hours=48)
        
        overview_logs = []
        for log in all_raw_logs:
            log_dt = parse_log_date(log['log_time'])
            if log_dt and log_dt >= cutoff_time:
                overview_logs.append({ "nickname": log['nickname'], "quantity": log['quantity'], "log_dt": log_dt })

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
            s_str = overview_logs[0]['log_dt'].strftime("%Y.%m.%d")
            e_str = overview_logs[-1]['log_dt'].strftime("%Y.%m.%d")
            date_range_str = s_str if s_str == e_str else f"{s_str} - {e_str}"
        else:
            date_range_str = "暂无数据"

        # --- B. 明细页数据 ---
        query_det = "SELECT id, log_time, nickname, item_type, quantity FROM logs"
        params_det = []
        if target_node_id:
            query_det += " WHERE device_id = ?"
            params_det.append(target_node_id)
        query_det += " ORDER BY id DESC LIMIT 5000"
        
        c.execute(query_det, params_det)
        raw_details = [dict(row) for row in c.fetchall()]
        details = []
        for log in raw_details:
            log_dt = parse_log_date(log['log_time'])
            if log_dt and log_dt >= cutoff_time:
                details.append(log)

        # --- C. 历史页数据 ---
        hist_sql = '''
            SELECT substr(l.log_time, 1, 10) as date_str, COUNT(DISTINCT l.nickname) as calc_users, SUM(l.quantity) as calc_sum, d.manual_users, d.manual_sum
            FROM logs l 
            LEFT JOIN daily_overrides d ON substr(l.log_time, 1, 10) = d.date AND d.device_id = l.device_id
            WHERE 1=1
        '''
        hist_params = []
        if target_node_id:
            hist_sql += " AND l.device_id = ?"
            hist_params.append(target_node_id)
            
        hist_sql += " GROUP BY date_str"
        
        c.execute(hist_sql, hist_params)
        raw_history = c.fetchall()
        history_list = []
        for row in raw_history:
            final_users = row['manual_users'] if row['manual_users'] is not None else row['calc_users']
            final_sum = row['manual_sum'] if row['manual_sum'] is not None else row['calc_sum']
            dt = parse_log_date(row['date_str'] + " 00:00:00")
            sort_key = dt if dt else datetime.min
            history_list.append({
                "date": row['date_str'],
                "user_count": final_users,
                "daily_sum": final_sum,
                "is_manual": row['manual_users'] is not None,
                "sort_key": sort_key
            })
        history_list.sort(key=lambda x: x['sort_key'], reverse=True)

    except Exception as e:
        print(f"Stats Error: {e}", flush=True)
        process_status_text = "Error"
        total_users, total_wins, rank_list, details, history_list = 0, 0, [], [], []
        date_range_str = "Error"
    
    conn.close()
    return jsonify({
        "process_status": process_status_text, 
        "total_users": total_users,
        "total_wins": total_wins,
        "rank_list": rank_list,
        "date_range": date_range_str,
        "details": details,
        "history_data": history_list
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
