from flask import Flask, request, jsonify, render_template, send_from_directory
import sqlite3
import re
import os
import sys
import time
import json
from datetime import datetime, timedelta

app = Flask(__name__)
app.jinja_env.variable_start_string = '[['
app.jinja_env.variable_end_string = ']]'

DB_PATH = '/app/data/lottery.db'
ROUND_SETTINGS_FILE = '/app/data/round_settings.json'

MONTH_MAP = {
    'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
}

LOG_PARSERS = {
    "default": {
        "name": "万花筒",
        "pattern": r"\[(.*?)\]\s+(.*?)_\d+\s+\|.*?[,，]\s*(?:.*?)[,，]\s*(\d+)",
        "item_type": "钻石"
    },
    "qilin": {
        "name": "麒麟",
        "pattern": r"\[(.*?)\]\s*恭喜\[(.*?)\].*?中了-(\d+)-",
        "item_type": "钻石"
    },
    "pixiu": {
        "name": "貔貅 (实物提取)",
        "pattern": r"^(.*?)----.*?----.*?----(.*?)----(.*)$",
        "item_type": "动态"
    }
}

def load_round_times():
    if os.path.exists(ROUND_SETTINGS_FILE):
        try:
            with open(ROUND_SETTINGS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except: return {}
    return {}

def save_round_times(data):
    try:
        with open(ROUND_SETTINGS_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, indent=4)
    except: pass

round_start_times = load_round_times()

def parse_log_date(date_str):
    try:
        date_str = date_str.strip()
        if '/' in date_str and re.search(r'[a-zA-Z]', date_str):
            parts = date_str.split(); d_parts = parts[0].split('/'); t_parts = parts[1].split(':')
            return datetime(int(d_parts[2]), MONTH_MAP.get(d_parts[1], 0), int(d_parts[0]), int(t_parts[0]), int(t_parts[1]), int(t_parts[2]))
        if '-' in date_str: return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        if '/' in date_str: return datetime.strptime(date_str, "%Y/%m/%d %H:%M:%S")
        if '.' in date_str: return datetime.strptime(date_str, "%Y.%m.%d %H:%M:%S")
        return None
    except: return None

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row; c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, log_time TEXT, nickname TEXT, item_type TEXT, quantity INTEGER, unique_sign TEXT UNIQUE, device_id TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS devices (device_id TEXT PRIMARY KEY, nickname TEXT, last_seen REAL, process_running INTEGER, first_seen REAL, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_overrides (date TEXT, device_id TEXT, manual_users INTEGER, manual_sum INTEGER, PRIMARY KEY (date, device_id))''')
    try: c.execute("ALTER TABLE devices ADD COLUMN template_id TEXT DEFAULT 'default'")
    except: pass
    try: c.execute("ALTER TABLE logs ADD COLUMN template_id TEXT DEFAULT 'default'")
    except: pass
    c.execute("PRAGMA table_info(daily_overrides)")
    columns = [col['name'] for col in c.fetchall()]
    if 'template_id' not in columns:
        c.execute("ALTER TABLE daily_overrides RENAME TO daily_overrides_old")
        c.execute('''CREATE TABLE daily_overrides (date TEXT, device_id TEXT, template_id TEXT DEFAULT 'default', manual_users INTEGER, manual_sum INTEGER, PRIMARY KEY (date, device_id, template_id))''')
        c.execute("INSERT INTO daily_overrides (date, device_id, manual_users, manual_sum) SELECT date, device_id, manual_users, manual_sum FROM daily_overrides_old")
        c.execute("DROP TABLE daily_overrides_old")
    try: c.execute('''DELETE FROM logs WHERE id NOT IN (SELECT MIN(id) FROM logs GROUP BY log_time, nickname, quantity, device_id)''')
    except: pass
    conn.commit(); conn.close()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    try: c = conn.cursor(); c.execute("SELECT 1 FROM devices LIMIT 1")
    except sqlite3.OperationalError: conn.close(); init_db(); conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    return conn

init_db()

def update_device_status(device_id, nickname, process_running, password):
    conn = get_db_connection(); c = conn.cursor(); now = time.time()
    c.execute("UPDATE devices SET nickname=?, last_seen=?, process_running=?, password=? WHERE device_id=?", (nickname, now, process_running, password, device_id))
    if c.rowcount == 0:
        c.execute("INSERT INTO devices (device_id, nickname, last_seen, process_running, first_seen, password, template_id) VALUES (?, ?, ?, ?, ?, ?, 'default')", (device_id, nickname, now, process_running, now, password))
    conn.commit(); conn.close()

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
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT * FROM devices ORDER BY first_seen ASC"); rows = c.fetchall()
    nodes = []; now = time.time()
    for r in rows:
        is_online = (now - r['last_seen']) < 15
        nodes.append({ "device_id": r['device_id'], "nickname": r['nickname'], "is_online": is_online, "process_running": bool(r['process_running']), "has_password": bool(r['password']), "template_id": r['template_id'] })
    conn.close()
    return jsonify({"nodes": nodes})

@app.route('/api/node/delete', methods=['POST'])
def delete_node():
    device_id = request.json.get('device_id')
    if not device_id: return jsonify({"status": "error"}), 400
    conn = get_db_connection()
    try: conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,)); conn.commit(); return jsonify({"status": "success"})
    except Exception as e: return jsonify({"error": str(e)}), 500
    finally: conn.close()

@app.route('/api/reset_round', methods=['POST'])
def reset_round():
    device_id = request.json.get('device_id')
    if not device_id: return jsonify({"status": "error"}), 400
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT template_id FROM devices WHERE device_id = ?", (device_id,))
    row = c.fetchone(); conn.close()
    template_id = row['template_id'] if row and row['template_id'] else 'default'
    key = f"{device_id}_{template_id}"
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    round_start_times[key] = now_str; save_round_times(round_start_times)
    return jsonify({"status": "success", "round_start_time": now_str})

@app.route('/api/templates', methods=['GET'])
def get_templates():
    tpls = [{"id": k, "name": v["name"]} for k, v in LOG_PARSERS.items()]
    return jsonify({"templates": tpls})

@app.route('/api/set_template', methods=['POST'])
def set_template():
    data = request.json
    device_id = data.get('node_id'); template_id = data.get('template_id')
    if not device_id or not template_id: return jsonify({"error": "Missing params"}), 400
    conn = get_db_connection()
    conn.execute("UPDATE devices SET template_id = ? WHERE device_id = ?", (template_id, device_id))
    conn.commit(); conn.close()
    return jsonify({"status": "success"})

@app.route('/api/history_logs')
def get_history_logs():
    target_node_id = request.args.get('node_id'); target_date = request.args.get('date') 
    if not target_node_id or not target_date: return jsonify({"logs": []})
    conn = get_db_connection(); c = conn.cursor()
    try:
        c.execute("SELECT template_id FROM devices WHERE device_id = ?", (target_node_id,))
        row = c.fetchone()
        template_id = row['template_id'] if row and row['template_id'] else 'default'
        target_date_slash = target_date.replace('-', '/')
        c.execute("SELECT log_time, nickname, item_type, quantity FROM logs WHERE device_id = ? AND template_id = ? AND (log_time LIKE ? OR log_time LIKE ?) ORDER BY id DESC", (target_node_id, template_id, f"{target_date}%", f"{target_date_slash}%"))
        return jsonify({"logs": [dict(row) for row in c.fetchall()]})
    except: return jsonify({"logs": []})
    finally: conn.close()

@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    data = request.json
    device_id = data.get('device_id'); nickname = data.get('nickname', 'Unknown'); password = data.get('password', '')
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
        c = conn.cursor()
        c.execute("SELECT template_id FROM devices WHERE device_id = ?", (device_id,))
        row = c.fetchone()
        template_id = row['template_id'] if row and row['template_id'] else 'default'
        c.execute("REPLACE INTO daily_overrides (date, device_id, template_id, manual_users, manual_sum) VALUES (?, ?, ?, ?, ?)", (data.get('date'), device_id, template_id, data.get('manual_users'), data.get('manual_sum')))
        conn.commit(); return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "msg": str(e)}), 500
    finally: conn.close()

@app.route('/api/user_total', methods=['GET'])
def get_user_total():
    target_node_id = request.args.get('node_id')
    nickname = request.args.get('nickname', '')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    calc_all = request.args.get('calc_all', '0')
    if not target_node_id: return jsonify({"error": "Missing node_id"}), 400
    conn = get_db_connection(); c = conn.cursor()
    try:
        c.execute("SELECT template_id FROM devices WHERE device_id = ?", (target_node_id,))
        row = c.fetchone()
        template_id = row['template_id'] if row and row['template_id'] else 'default'

        start_dt = datetime.min
        end_dt = datetime.max
        if start_date:
            start_str = start_date.replace('T', ' ')
            if len(start_str) == 10: start_str += " 00:00:00"
            elif len(start_str) == 16: start_str += ":00"
            start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
        if end_date:
            end_str = end_date.replace('T', ' ')
            if len(end_str) == 10: end_str += " 23:59:59"
            elif len(end_str) == 16: end_str += ":59"
            end_dt = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")

        if calc_all == '1':
            c.execute("SELECT log_time, quantity FROM logs WHERE device_id = ? AND template_id = ?", (target_node_id, template_id))
            rows = c.fetchall(); total = 0
            for r in rows:
                log_dt = parse_log_date(r['log_time'])
                if log_dt and start_dt <= log_dt <= end_dt: total += r['quantity']
            return jsonify({"total": total})
        elif not nickname:
            c.execute("SELECT DISTINCT nickname FROM logs WHERE device_id = ? AND template_id = ?", (target_node_id, template_id))
            return jsonify({"users": [r['nickname'] for r in c.fetchall() if r['nickname']]})
        else:
            if start_date or end_date:
                c.execute("SELECT log_time, quantity FROM logs WHERE device_id = ? AND nickname = ? AND template_id = ?", (target_node_id, nickname, template_id))
                rows = c.fetchall(); total = 0
                for r in rows:
                    log_dt = parse_log_date(r['log_time'])
                    if log_dt and start_dt <= log_dt <= end_dt: total += r['quantity']
                return jsonify({"total": total})
            else:
                c.execute("SELECT SUM(quantity) as total FROM logs WHERE device_id = ? AND nickname = ? AND template_id = ?", (target_node_id, nickname, template_id))
                r = c.fetchone()
                return jsonify({"total": r['total'] if r['total'] else 0})
    except Exception as e: return jsonify({"error": str(e)}), 500
    finally: conn.close()

@app.route('/upload', methods=['POST'])
def upload_file():
    sys.stdout.flush()
    file = request.files.get('file'); device_id = request.form.get('device_id')
    nickname = request.form.get('nickname', 'Unknown'); password = request.form.get('password', '')
    process_running = 1 if request.form.get('process_running', 'False') == 'True' else 0
    
    # 🔥 核心：无条件信任客户端探测出来的模板
    client_template = request.form.get('template_id', 'default')
    
    if not file or not device_id: return jsonify({"status": "error"}), 400
    update_device_status(device_id, nickname, process_running, password)
    
    conn = get_db_connection(); c = conn.cursor()
    parser = LOG_PARSERS.get(client_template, LOG_PARSERS['default'])
    pattern = parser['pattern']
    
    raw_data = file.read()
    try: content = raw_data.decode('gb18030')
    except: content = raw_data.decode('utf-8', errors='ignore')
    lines = content.split('\n')
    
    new_count = 0
    for line in lines:
        line = line.strip()
        if not line: continue 
        match = re.search(pattern, line)
        if match:
            if client_template == 'pixiu':
                log_time_raw, nick, raw_val = match.groups()
                log_time = log_time_raw.replace('年', '-').replace('月', '-').replace('日', '').replace('时', ':').replace('分', ':').replace('秒', '')
                if '钻' in raw_val:
                    q_match = re.search(r'\d+', raw_val)
                    quantity = int(q_match.group()) if q_match else 1
                    final_item_type = "钻石"
                else:
                    final_item_type = raw_val
                    quantity = 1
            else:
                log_time, nick, q_str = match.group(1), match.group(2), match.group(3)
                quantity = int(q_str)
                final_item_type = parser['item_type']
            
            unique_sign = f"{log_time}_{nick}_{final_item_type}_{quantity}_{device_id}" 
            try:
                c.execute("INSERT INTO logs (log_time, nickname, item_type, quantity, unique_sign, device_id, template_id) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                          (log_time, nick, final_item_type, quantity, unique_sign, device_id, client_template))
                new_count += 1
            except sqlite3.IntegrityError: pass 
            
    conn.commit(); conn.close()
    return jsonify({"status": "success", "new_entries": new_count})

@app.route('/api/stats')
def get_stats():
    target_node_id = request.args.get('node_id'); req_password = request.args.get('password', '')
    conn = get_db_connection(); c = conn.cursor()
    try:
        process_status_text = "未连接"
        current_template = "default"
        if target_node_id:
            try:
                c.execute("SELECT last_seen, process_running, password, template_id FROM devices WHERE device_id = ?", (target_node_id,))
                row = c.fetchone()
                if row:
                    if row['password'] and row['password'] != req_password:
                        conn.close(); return jsonify({"error": "auth_failed"}), 403
                    current_template = row['template_id']
                    if (time.time() - row['last_seen']) >= 15: process_status_text = "离线" 
                    elif row['process_running']: process_status_text = "运行中"
                    else: process_status_text = "未运行"
                else: process_status_text = "未知设备"
            except sqlite3.OperationalError: process_status_text = "数据异常"
        else: process_status_text = "请选择节点"

        query = "SELECT id, log_time, nickname, quantity FROM logs WHERE device_id = ? AND template_id = ?"
        c.execute(query, (target_node_id, current_template))
        all_raw_logs = [dict(row) for row in c.fetchall()]

        now = datetime.now()
        base_cutoff = now - timedelta(hours=48); cutoff_time = base_cutoff
        
        key = f"{target_node_id}_{current_template}"
        if key in round_start_times:
            try: cutoff_time = max(base_cutoff, datetime.strptime(round_start_times[key], '%Y-%m-%d %H:%M:%S'))
            except: pass

        overview_logs = []
        for log in all_raw_logs:
            log_dt = parse_log_date(log['log_time'])
            if log_dt and log_dt >= cutoff_time: overview_logs.append({ "nickname": log['nickname'], "quantity": log['quantity'], "log_dt": log_dt })

        total_users = len(set(l['nickname'] for l in overview_logs))
        total_wins = sum(l['quantity'] for l in overview_logs)
        
        rank_map = {}
        for l in overview_logs:
            if l['nickname'] not in rank_map: rank_map[l['nickname']] = {"win_times": 0, "win_sum": 0}
            rank_map[l['nickname']]["win_times"] += 1; rank_map[l['nickname']]["win_sum"] += l['quantity']
        
        rank_list = [{"nickname": k, "win_times": v["win_times"], "win_sum": v["win_sum"]} for k, v in rank_map.items()]
        rank_list.sort(key=lambda x: x['win_sum'], reverse=True)

        date_range_str = f"{cutoff_time.strftime('%m-%d %H:%M')} - 至今"
        if not overview_logs: date_range_str = "暂无数据"

        query_det = "SELECT id, log_time, nickname, item_type, quantity FROM logs WHERE device_id = ? AND template_id = ? ORDER BY id DESC LIMIT 5000"
        c.execute(query_det, (target_node_id, current_template))
        details = [log for log in [dict(row) for row in c.fetchall()] if parse_log_date(log['log_time']) and parse_log_date(log['log_time']) >= cutoff_time]

        hist_sql = '''SELECT substr(l.log_time, 1, 10) as date_str, COUNT(DISTINCT l.nickname) as calc_users, SUM(l.quantity) as calc_sum, d.manual_users, d.manual_sum 
                      FROM logs l 
                      LEFT JOIN daily_overrides d ON substr(l.log_time, 1, 10) = d.date AND d.device_id = l.device_id AND d.template_id = l.template_id
                      WHERE l.device_id = ? AND l.template_id = ?
                      GROUP BY date_str'''
        c.execute(hist_sql, (target_node_id, current_template))
        
        history_list = []
        today_strs = (now.strftime('%Y-%m-%d'), now.strftime('%Y/%m/%d'), now.strftime('%Y.%m.%d'))
        for row in c.fetchall():
            if row['date_str'] in today_strs: continue
            final_users = row['manual_users'] if row['manual_users'] is not None else row['calc_users']
            final_sum = row['manual_sum'] if row['manual_sum'] is not None else row['calc_sum']
            dt = parse_log_date(row['date_str'] + " 00:00:00")
            history_list.append({ "date": row['date_str'], "user_count": final_users, "daily_sum": final_sum, "is_manual": row['manual_users'] is not None, "sort_key": dt if dt else datetime.min })
        history_list.sort(key=lambda x: x['sort_key'], reverse=True)

    except Exception as e:
        print(f"Stats Error: {e}", flush=True)
        process_status_text, total_users, total_wins, rank_list, details, history_list, current_template = "Error", 0, 0, [], [], [], "default"
        date_range_str = "Error"
    
    conn.close()
    return jsonify({
        "process_status": process_status_text, "current_template": current_template,
        "total_users": total_users, "total_wins": total_wins, "rank_list": rank_list,
        "date_range": date_range_str, "details": details, "history_data": history_list
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)