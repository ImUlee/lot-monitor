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
# 🔥 改为字典，存储 { "client_id": timestamp }
LAST_HEARTBEATS = {} 

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
    
    # 🔥 1. 创建 logs 表 (带 client_id)
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        log_time TEXT, 
        nickname TEXT, 
        item_type TEXT, 
        quantity INTEGER, 
        unique_sign TEXT UNIQUE,
        client_id TEXT
    )''')
    
    # 🔥 2. 尝试添加 client_id 列 (兼容旧数据库)
    try:
        c.execute("ALTER TABLE logs ADD COLUMN client_id TEXT")
    except: pass

    # 🔥 3. 创建历史修正表 (联合主键: date + client_id)
    c.execute('''CREATE TABLE IF NOT EXISTS daily_overrides (
        date TEXT, 
        client_id TEXT,
        manual_users INTEGER, 
        manual_sum INTEGER,
        PRIMARY KEY (date, client_id)
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

# 🔥 获取节点列表接口
@app.route('/api/nodes')
def get_nodes():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # 获取所有有数据的节点
    c.execute("SELECT DISTINCT client_id FROM logs WHERE client_id IS NOT NULL AND client_id != ''")
    nodes = [row[0] for row in c.fetchall()]
    conn.close()
    
    # 加上当前在线但可能还没数据的节点
    for node in LAST_HEARTBEATS.keys():
        if node not in nodes: nodes.append(node)
    
    return jsonify({"nodes": sorted(nodes)})

@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    data = request.json
    client_id = data.get('client_id', 'Unknown')
    # 🔥 记录具体节点的最后在线时间
    LAST_HEARTBEATS[client_id] = time.time()
    return jsonify({"status": "ok"})

@app.route('/api/health', methods=['GET'])
def health_check(): return jsonify({"status": "online", "server": "LittlePilot"})

@app.route('/api/update_history', methods=['POST'])
def update_history():
    data = request.json
    client_id = data.get('client_id', 'Unknown')
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("REPLACE INTO daily_overrides (date, client_id, manual_users, manual_sum) VALUES (?, ?, ?, ?)", 
                     (data.get('date'), client_id, data.get('manual_users'), data.get('manual_sum')))
        conn.commit()
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "msg": str(e)}), 500
    finally: conn.close()

@app.route('/upload', methods=['POST'])
def upload_file():
    sys.stdout.flush()
    file = request.files.get('file')
    # 🔥 获取客户端上传的 ID
    client_id = request.form.get('client_id', 'Unknown')
    
    if not file: return jsonify({"status": "error"}), 400
    raw_data = file.read()
    try: content = raw_data.decode('gb18030')
    except: content = raw_data.decode('utf-8', errors='ignore')
    
    lines = content.split('\n')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    new_count = 0
    pattern = r"\[(.*?)\]\s+(.*?)_\d+\s+\|.*?[,，]\s*(?:.*?)[,，]\s*(\d+)"
    
    for line in lines:
        line = line.strip()
        if not line: continue 
        match = re.search(pattern, line)
        if match:
            log_time, nickname, quantity = match.group(1), match.group(2), int(match.group(3))
            unique_sign = f"{log_time}_{nickname}_{quantity}_{client_id}" # 🔥 唯一标识加入 client_id 防止冲突
            try:
                c.execute("INSERT INTO logs (log_time, nickname, item_type, quantity, unique_sign, client_id) VALUES (?, ?, ?, ?, ?, ?)", 
                          (log_time, nickname, "钻石", quantity, unique_sign, client_id))
                new_count += 1
            except sqlite3.IntegrityError: pass 
    conn.commit()
    conn.close()
    
    # 顺便更新一下心跳
    LAST_HEARTBEATS[client_id] = time.time()
    
    return jsonify({"status": "success", "new_entries": new_count})

@app.route('/api/stats')
def get_stats():
    # 🔥 获取前端选择的节点
    target_node = request.args.get('node')
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    try:
        # 状态判断
        last_seen = LAST_HEARTBEATS.get(target_node, 0)
        is_online = (time.time() - last_seen) < 15 and target_node is not None

        # --- A. 总览页数据 (带 filter) ---
        query = "SELECT id, log_time, nickname, quantity FROM logs"
        params = []
        if target_node:
            query += " WHERE client_id = ?"
            params.append(target_node)
            
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

        # --- B. 明细页数据 (带 filter) ---
        query_det = "SELECT id, log_time, nickname, item_type, quantity FROM logs"
        params_det = []
        if target_node:
            query_det += " WHERE client_id = ?"
            params_det.append(target_node)
        query_det += " ORDER BY id DESC LIMIT 5000"
        
        c.execute(query_det, params_det)
        raw_details = [dict(row) for row in c.fetchall()]
        details = []
        for log in raw_details:
            log_dt = parse_log_date(log['log_time'])
            if log_dt and log_dt >= cutoff_time:
                details.append(log)

        # --- C. 历史页数据 (带 filter & 联表查询) ---
        # 这里的 SQL 需要关联 client_id
        hist_sql = '''
            SELECT substr(l.log_time, 1, 10) as date_str, COUNT(DISTINCT l.nickname) as calc_users, SUM(l.quantity) as calc_sum, d.manual_users, d.manual_sum
            FROM logs l 
            LEFT JOIN daily_overrides d ON substr(l.log_time, 1, 10) = d.date AND d.client_id = l.client_id
            WHERE 1=1
        '''
        hist_params = []
        if target_node:
            hist_sql += " AND l.client_id = ?"
            hist_params.append(target_node)
            
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
