from flask import Flask, request, jsonify, render_template, send_from_directory
import sqlite3
import re
import os
import sys
import time
import json
from datetime import datetime, timedelta

app = Flask(__name__)

# 避免和 Vue 的 {{ }} 冲突
app.jinja_env.variable_start_string = '[['
app.jinja_env.variable_end_string = ']]'

DB_PATH = '/app/data/lottery.db'
# 🔥 将轮次配置文件放在 /app/data/ 目录下，保证 Docker 重启不丢失
ROUND_SETTINGS_FILE = '/app/data/round_settings.json'

MONTH_MAP = {
    'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
}

# ==========================================
# 🔥 新增：轮次时间持久化配置
# ==========================================
def load_round_times():
    if os.path.exists(ROUND_SETTINGS_FILE):
        try:
            with open(ROUND_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_round_times(data):
    try:
        with open(ROUND_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Failed to save round times: {e}")

round_start_times = load_round_times()


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
    print("🔄 Initializing Database...", flush=True)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, log_time TEXT, nickname TEXT, item_type TEXT, quantity INTEGER, unique_sign TEXT UNIQUE, device_id TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS devices (device_id TEXT PRIMARY KEY, nickname TEXT, last_seen REAL, process_running INTEGER, first_seen REAL, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_overrides (date TEXT, device_id TEXT, manual_users INTEGER, manual_sum INTEGER, PRIMARY KEY (date, device_id))''')
    conn.commit()
    conn.close()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        c = conn.cursor()
        c.execute("SELECT 1 FROM devices LIMIT 1")
    except sqlite3.OperationalError:
        print("⚠️ Database tables missing. Re-creating...", flush=True)
        conn.close()
        init_db()
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    return conn

init_db()

def update_device_status(device_id, nickname, process_running, password):
    conn = get_db_connection()
    c = conn.cursor()
    now = time.time()
    c.execute("UPDATE devices SET nickname=?, last_seen=?, process_running=?, password=? WHERE device_id=?", 
              (nickname, now, process_running, password, device_id))
    if c.rowcount == 0:
        c.execute("INSERT INTO devices (device_id, nickname, last_seen, process_running, first_seen, password) VALUES (?, ?, ?, ?, ?, ?)", 
                  (device_id, nickname, now, process_running, now, password))
    conn.commit()
    conn.close()

@app.route('/manifest.json')
def serve_manifest(): return send_from_directory('static', 'manifest.json', mimetype='application/json')
@app.route('/sw.js')
def serve_sw(): return send_from_directory('static', 'sw.js', mimetype='application/javascript')
@app.route('/static/<path:path>')
def send_static(path): return send_from_directory('static', path)
@app.route('/')
def dashboard(): return render_template('dashboard.html')

@app.route('/api/nodes')
def get_nodes():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM devices ORDER BY first_seen ASC")
    rows = c.fetchall()
    nodes = []
    now = time.time()
    for r in rows:
        is_online = (now - r['last_seen']) < 15
        nodes.append({
            "device_id": r['device_id'],
            "nickname": r['nickname'],
            "is_online": is_online,
            "process_running": bool(r['process_running']),
            "has_password": bool(r['password'])
        })
    conn.close()
    return jsonify({"nodes": nodes})

@app.route('/api/node/delete', methods=['POST'])
def delete_node():
    data = request.json
    device_id = data.get('device_id')
    if not device_id: return jsonify({"status": "error"}), 400
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
        conn.commit()
        # 清理轮次记录
        if device_id in round_start_times:
            del round_start_times[device_id]
            save_round_times(round_start_times)
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"error": str(e)}), 500
    finally: conn.close()

# 🔥 新增：手动清空重置本轮
@app.route('/api/reset_round', methods=['POST'])
def reset_round():
    data = request.json
    device_id = data.get('device_id')
    if not device_id: 
        return jsonify({"status": "error", "message": "Missing device_id"}), 400
        
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    round_start_times[device_id] = now_str
    save_round_times(round_start_times)
    
    return jsonify({"status": "success", "round_start_time": now_str})


@app.route('/api/history_logs')
def get_history_logs():
    target_node_id = request.args.get('node_id')
    target_date = request.args.get('date') 
    
    if not target_node_id or not target_date:
        return jsonify({"logs": []})

    conn = get_db_connection()
    c = conn.cursor()
    try:
        target_date_slash = target_date.replace('-', '/')
        query = """
            SELECT log_time, nickname, quantity 
            FROM logs 
            WHERE device_id = ? 
            AND (log_time LIKE ? OR log_time LIKE ?)
            ORDER BY id DESC
        """
        c.execute(query, (target_node_id, f"{target_date}%", f"{target_date_slash}%"))
        rows = [dict(row) for row in c.fetchall()]
        return jsonify({"logs": rows})
    except Exception as e:
        print(f"History Logs Error: {e}")
        return jsonify({"logs": []})
    finally:
        conn.close()

@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    data = request.json
    device_id = data.get('device_id')
    nickname = data.get('nickname', 'Unknown')
    password = data.get('password', '')
    process_running = 1 if data.get('process_running', False) else 0
    if not device_id: return jsonify({"status": "error"}), 400
    try:
        update_device_status(device_id, nickname, process_running, password)
        return jsonify({"status": "ok"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/health', methods=['GET'])
def health_check(): return jsonify({"status": "online", "server": "LittlePilot"})

@app.route('/api/update_history', methods=['POST'])
def update_history():
    data = request.json
    device_id = data.get('device_id')
    conn = get_db_connection()
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
    password = request.form.get('password', '')
    process_status_str = request.form.get('process_running', 'False')
    process_running = 1 if process_status_str == 'True' else 0
    
    if not file or not device_id: return jsonify({"status": "error"}), 400
    
    update_device_status(device_id, nickname, process_running, password)
    
    raw_data = file.read()
    try: content = raw_data.decode('gb18030')
    except: content = raw_data.decode('utf-8', errors='ignore')
    lines = content.split('\n')
    conn = get_db_connection()
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
    req_password = request.args.get('password', '')
    conn = get_db_connection()
    c = conn.cursor()
    try:
        process_status_text = "未连接"
        is_client_online = False
        if target_node_id:
            try:
                c.execute("SELECT last_seen, process_running, password FROM devices WHERE device_id = ?", (target_node_id,))
                row = c.fetchone()
                if row:
                    db_pass = row['password']
                    if db_pass and db_pass != req_password:
                        conn.close()
                        return jsonify({"error": "auth_failed"}), 403
                    is_client_online = (time.time() - row['last_seen']) < 15
                    if not is_client_online: process_status_text = "离线" 
                    elif row['process_running']: process_status_text = "运行中"
                    else: process_status_text = "未运行"
                else: process_status_text = "未知设备"
            except sqlite3.OperationalError: process_status_text = "数据异常"
        else: process_status_text = "请选择节点"

        query = "SELECT id, log_time, nickname, quantity FROM logs"
        params = []
        if target_node_id:
            query += " WHERE device_id = ?"
            params.append(target_node_id)
        c.execute(query, params)
        all_raw_logs = [dict(row) for row in c.fetchall()]

        # ==========================================
        # 🔥 核心：48小时保底 & 手动重置过滤逻辑
        # ==========================================
        now = datetime.now()
        base_cutoff = now - timedelta(hours=48)
        cutoff_time = base_cutoff
        
        # 提取当前设备的手动重置时间
        if target_node_id and target_node_id in round_start_times:
            try:
                reset_time = datetime.strptime(round_start_times[target_node_id], '%Y-%m-%d %H:%M:%S')
                # 取 【48小时前】 和 【点击重置的时间】 之间更晚的一个
                cutoff_time = max(base_cutoff, reset_time)
            except Exception:
                pass

        overview_logs = []
        for log in all_raw_logs:
            log_dt = parse_log_date(log['log_time'])
            # 过滤：只有时间大于 cutoff_time 的数据才会显示在本轮
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

        # 动态显示时间范围：显示重置时间到现在的范围
        display_time = cutoff_time.strftime("%m-%d %H:%M")
        date_range_str = f"{display_time} - 至今"
        if not overview_logs:
            date_range_str = "暂无数据"

        # 明细页面：同样遵循重置时间过滤
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

        # ==========================================
        # 📅 历史记录：不受重置影响，原样展示所有天数
        # ==========================================
        hist_sql = '''SELECT substr(l.log_time, 1, 10) as date_str, COUNT(DISTINCT l.nickname) as calc_users, SUM(l.quantity) as calc_sum, d.manual_users, d.manual_sum FROM logs l LEFT JOIN daily_overrides d ON substr(l.log_time, 1, 10) = d.date AND d.device_id = l.device_id WHERE 1=1'''
        hist_params = []
        if target_node_id:
            hist_sql += " AND l.device_id = ?"
            hist_params.append(target_node_id)
        hist_sql += " GROUP BY date_str"
        c.execute(hist_sql, hist_params)
        raw_history = c.fetchall()
        history_list = []
        
        # 获取当前日期字符串，排除“今天”
        today_str = now.strftime('%Y-%m-%d')
        today_str_slash = now.strftime('%Y/%m/%d')
        today_str_dot = now.strftime('%Y.%m.%d')
        
        for row in raw_history:
            date_str = row['date_str']
            # 不在历史记录里显示当天的数据
            if date_str in (today_str, today_str_slash, today_str_dot):
                continue
                
            final_users = row['manual_users'] if row['manual_users'] is not None else row['calc_users']
            final_sum = row['manual_sum'] if row['manual_sum'] is not None else row['calc_sum']
            dt = parse_log_date(row['date_str'] + " 00:00:00")
            sort_key = dt if dt else datetime.min
            history_list.append({ "date": row['date_str'], "user_count": final_users, "daily_sum": final_sum, "is_manual": row['manual_users'] is not None, "sort_key": sort_key })
        
        history_list.sort(key=lambda x: x['sort_key'], reverse=True)

    except Exception as e:
        print(f"Stats Error: {e}", flush=True)
        process_status_text, total_users, total_wins, rank_list, details, history_list = "Error", 0, 0, [], [], []
        date_range_str = "Error"
    
    conn.close()
    return jsonify({
        "process_status": process_status_text, 
        "total_users": total_users, "total_wins": total_wins, "rank_list": rank_list,
        "date_range": date_range_str, "details": details, "history_data": history_list
    })
# ==========================================
# 🔥 升级：获取历史所有昵称 & 按时间范围查询指定昵称历史总和
# ==========================================
@app.route('/api/user_total', methods=['GET'])
def get_user_total():
    target_node_id = request.args.get('node_id')
    nickname = request.args.get('nickname', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    
    if not target_node_id:
        return jsonify({"error": "Missing node_id"}), 400
        
    conn = get_db_connection()
    c = conn.cursor()
    try:
        if not nickname:
            c.execute("SELECT DISTINCT nickname FROM logs WHERE device_id = ?", (target_node_id,))
            users = [row['nickname'] for row in c.fetchall() if row['nickname']]
            return jsonify({"users": users})
        else:
            # 如果传了时间范围，就在 Python 中精确解析时间并累加
            if start_date or end_date:
                c.execute("SELECT log_time, quantity FROM logs WHERE device_id = ? AND nickname = ?", (target_node_id, nickname))
                rows = c.fetchall()
                total = 0
                
                # 转换边界时间
                start_dt = datetime.strptime(start_date + " 00:00:00", "%Y-%m-%d %H:%M:%S") if start_date else datetime.min
                end_dt = datetime.strptime(end_date + " 23:59:59", "%Y-%m-%d %H:%M:%S") if end_date else datetime.max
                
                for row in rows:
                    log_dt = parse_log_date(row['log_time'])
                    if log_dt and start_dt <= log_dt <= end_dt:
                        total += row['quantity']
                return jsonify({"total": total})
            else:
                # 没传时间，直接利用 SQL 算所有总和，效率最高
                c.execute("SELECT SUM(quantity) as total FROM logs WHERE device_id = ? AND nickname = ?", (target_node_id, nickname))
                row = c.fetchone()
                total = row['total'] if row['total'] else 0
                return jsonify({"total": total})
    except Exception as e:
        print(f"User Total Error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)